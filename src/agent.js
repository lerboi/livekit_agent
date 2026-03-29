/**
 * Voco LiveKit Voice Agent
 *
 * Main entry point for the AI receptionist agent.
 * Replaces the Retell WebSocket server + Groq pipeline with:
 *   Twilio SIP → LiveKit → Gemini 3.1 Flash Live (native audio-to-audio)
 *
 * Architecture:
 * - Each inbound call creates a LiveKit room via SIP dispatch rule
 * - This agent joins the room, looks up the tenant, and opens a Gemini Live session
 * - All 6 tools execute in-process (no webhook round-trips)
 * - Post-call pipeline runs immediately when the session closes
 */

import * as Sentry from '@sentry/node';

Sentry.init({
  dsn: process.env.SENTRY_DSN,
  tracesSampleRate: 0.1,
  environment: process.env.NODE_ENV || 'production',
});

import { defineAgent, cli, ServerOptions, voice } from '@livekit/agents';
import * as google from '@livekit/agents-plugin-google';
import { EgressClient, RoomServiceClient } from 'livekit-server-sdk';
import { buildSystemPrompt } from './prompt.js';
import { createTools } from './tools/index.js';
import { getSupabaseAdmin } from './supabase.js';
import { runPostCallPipeline } from './post-call.js';
import { calculateInitialSlots } from './utils.js';
import { startHealthServer } from './health.js';

// Start health check server (non-blocking, separate port)
startHealthServer();

// Voice mapping: tone_preset → Gemini voice name
const VOICE_MAP = {
  professional: 'Kore',
  friendly: 'Aoede',
  local_expert: 'Achird',
};

// Subscription statuses that block inbound calls (matches main repo's subscription-gate.js)
const BLOCKED_STATUSES = ['canceled', 'paused', 'incomplete'];

// Timeout for SIP participant to join the room (ms)
const PARTICIPANT_TIMEOUT_MS = 30_000;

export default defineAgent({
  entry: async (ctx) => {
    try {
      // ── Connect to room ──
      await ctx.connect();
      const callId = ctx.room.name;
      console.log(`[agent] Connected to room: ${callId}`);

      // ── Wait for SIP participant (with timeout) ──
      const participant = await Promise.race([
        ctx.waitForParticipant(),
        new Promise((_, reject) =>
          setTimeout(() => reject(new Error('SIP participant did not join within 30s')), PARTICIPANT_TIMEOUT_MS),
        ),
      ]);

      // Extract phone numbers from SIP participant attributes
      // sip.trunkPhoneNumber = the Twilio number being called (used for tenant lookup)
      // sip.phoneNumber = the caller's number
      const toNumber =
        participant.attributes?.['sip.trunkPhoneNumber'] ||
        participant.attributes?.['sip.to'] ||
        '';
      const fromNumber =
        participant.attributes?.['sip.phoneNumber'] ||
        participant.attributes?.['sip.from'] ||
        '';
      const sipParticipantIdentity = participant.identity || '';

      // Check if this is a test call (metadata set by test-call route)
      let isTestCall = false;
      try {
        const roomMeta = ctx.room.metadata ? JSON.parse(ctx.room.metadata) : {};
        isTestCall = roomMeta.test_call === true;
      } catch {}

      console.log(`[agent] Call started: room=${callId} from=${fromNumber} to=${toNumber} test=${isTestCall}`);

      // ── Tenant lookup ──
      const supabase = getSupabaseAdmin();
      const { data: tenant, error: tenantError } = await supabase
        .from('tenants')
        .select('*')
        .eq('phone_number', toNumber)
        .single();

      if (tenantError) {
        console.warn(`[agent] Tenant lookup failed for ${toNumber}: ${tenantError.message}`);
      }

      const tenantId = tenant?.id ?? null;
      const onboardingComplete = tenant?.onboarding_complete ?? false;
      const businessName = tenant?.business_name ?? 'Voco';
      const locale = tenant?.default_locale ?? 'en';
      const tonePreset = tenant?.tone_preset ?? 'professional';
      const ownerPhone = tenant?.owner_phone ?? null;
      const tenantTimezone = tenant?.tenant_timezone ?? 'America/Chicago';

      console.log(`[agent] Tenant: ${tenantId ?? 'NONE'} (${businessName})`);

      // ── Subscription gate (fail-open) ──
      if (tenantId) {
        const { data: sub, error: subError } = await supabase
          .from('subscriptions')
          .select('status')
          .eq('tenant_id', tenantId)
          .eq('is_current', true)
          .maybeSingle();

        if (!subError && sub?.status && BLOCKED_STATUSES.includes(sub.status)) {
          console.log(`[agent] Subscription blocked: tenant=${tenantId} status=${sub.status} — disconnecting caller`);
          try {
            const roomService = new RoomServiceClient(
              process.env.LIVEKIT_URL,
              process.env.LIVEKIT_API_KEY,
              process.env.LIVEKIT_API_SECRET,
            );
            await roomService.removeParticipant(callId, sipParticipantIdentity);
          } catch (err) {
            console.error('[agent] Failed to disconnect blocked caller:', err.message);
          }
          return;
        }

        if (subError) {
          console.warn(`[agent] Subscription check failed (allowing call): ${subError.message}`);
        }
      }

      // ── Calculate available slots ──
      let availableSlots = '';
      if (onboardingComplete && tenantId) {
        try {
          availableSlots = await calculateInitialSlots(supabase, tenant);
        } catch (err) {
          console.error('[agent] Slot calculation failed:', err.message);
        }
      }

      // ── Fetch intake questions ──
      let intakeQuestions = '';
      if (tenantId) {
        const { data: services } = await supabase
          .from('services')
          .select('intake_questions')
          .eq('tenant_id', tenantId)
          .eq('is_active', true);
        if (services) {
          intakeQuestions = services
            .flatMap((s) => s.intake_questions || [])
            .filter((q, i, arr) => arr.indexOf(q) === i)
            .join('\n');
        }
      }

      // ── Build system prompt ──
      let systemPrompt = buildSystemPrompt(locale, {
        business_name: businessName,
        onboarding_complete: onboardingComplete,
        tone_preset: tonePreset,
        intake_questions: intakeQuestions,
      });
      if (availableSlots) {
        systemPrompt += `\n\nAVAILABLE APPOINTMENT SLOTS:\n${availableSlots}`;
      }

      // ── Create call record (only when tenant exists — calls.tenant_id is NOT NULL) ──
      const startTimestamp = Date.now();
      let callRecord = null;

      if (tenantId) {
        const { data, error: callError } = await supabase
          .from('calls')
          .upsert(
            {
              call_id: callId,
              tenant_id: tenantId,
              from_number: fromNumber,
              to_number: toNumber,
              direction: 'inbound',
              status: 'started',
              start_timestamp: startTimestamp,
              call_provider: 'livekit',
            },
            { onConflict: 'call_id' },
          )
          .select('id')
          .single();

        if (callError) {
          console.error('[agent] Call record insert failed:', callError.message);
        }
        callRecord = data;
      } else {
        console.warn(`[agent] No tenant for ${toNumber} — skipping call record (tenant_id is NOT NULL)`);
      }

      // ── Create tools (in-process, direct Supabase access) ──
      const tools = createTools({
        supabase,
        tenant,
        tenantId,
        callId,
        callUuid: callRecord?.id || null,
        fromNumber,
        toNumber,
        ownerPhone,
        startTimestamp,
        onboardingComplete,
        tenantTimezone,
        roomName: callId,
        sipParticipantIdentity,
        ctx,
      });

      // ── Create Gemini model + agent + session ──
      const voiceName = VOICE_MAP[tonePreset] || 'Kore';

      const model = new google.beta.realtime.RealtimeModel({
        model: 'gemini-3.1-flash-live-preview',
        voice: voiceName,
        temperature: 0.3,
        instructions: systemPrompt,
      });

      const agent = new voice.Agent({
        instructions: systemPrompt,
        tools,
      });

      const session = new voice.AgentSession({
        llm: model,
      });

      // ── Session error handler (visibility into mid-call Gemini errors) ──
      session.on('error', (event) => {
        console.error(`[agent] Session error: room=${callId} tenant=${tenantId}`, event.error);
        Sentry.captureException(event.error, { tags: { callId, tenantId } });
      });

      // ── Collect transcript in real-time ──
      const transcriptTurns = [];

      session.on('conversation_item_added', (event) => {
        const text = event.item?.textContent || event.item?.text || event.text;
        if (text) {
          transcriptTurns.push({
            role: event.item?.role === 'user' ? 'user' : 'agent',
            content: text,
            timestamp: Date.now(),
          });
        }
      });

      // ── Start session ──
      await session.start({ agent, room: ctx.room });
      console.log(`[agent] Session started: room=${callId}`);

      // ── Start Egress recording (before greeting so full call is captured) ──
      let egressId;
      const recordingPath = `${callId}.mp4`;
      try {
        const egressClient = new EgressClient(
          process.env.LIVEKIT_URL,
          process.env.LIVEKIT_API_KEY,
          process.env.LIVEKIT_API_SECRET,
        );

        const egressInfo = await egressClient.startRoomCompositeEgress(
          callId,
          {
            file: {
              filepath: recordingPath,
              output: {
                case: 's3',
                value: {
                  accessKey: process.env.SUPABASE_S3_ACCESS_KEY,
                  secret: process.env.SUPABASE_S3_SECRET_KEY,
                  bucket: 'call-recordings',
                  region: process.env.SUPABASE_S3_REGION || 'ap-northeast-1',
                  endpoint: process.env.SUPABASE_S3_ENDPOINT,
                  forcePathStyle: true,
                },
              },
            },
          },
          { audioOnly: true },
        );
        egressId = egressInfo.egressId;
        console.log(`[agent] Egress started: ${egressId}`);

        // Store egress ID in call record
        if (callRecord) {
          await supabase.from('calls').update({ egress_id: egressId }).eq('call_id', callId);
        }
      } catch (err) {
        console.error('[agent] Failed to start egress:', err.message);
      }

      // ── Generate greeting ──
      session.generateReply();

      // ── Handle session end (post-call pipeline) ──
      session.on('close', async () => {
        const endTimestamp = Date.now();
        const durationSec = Math.round((endTimestamp - startTimestamp) / 1000);
        console.log(`[agent] Session closed: room=${callId} duration=${durationSec}s`);

        // Stop egress
        if (egressId) {
          try {
            const egressClient = new EgressClient(
              process.env.LIVEKIT_URL,
              process.env.LIVEKIT_API_KEY,
              process.env.LIVEKIT_API_SECRET,
            );
            await egressClient.stopEgress(egressId);
          } catch (err) {
            console.error('[agent] Failed to stop egress:', err.message);
          }
        }

        // Run post-call pipeline
        try {
          await runPostCallPipeline({
            supabase,
            callId,
            callUuid: callRecord?.id || null,
            tenantId,
            tenant,
            fromNumber,
            toNumber,
            startTimestamp,
            endTimestamp,
            transcriptTurns,
            recordingStoragePath: egressId ? recordingPath : null,
            isTestCall,
          });
        } catch (err) {
          console.error('[agent] Post-call pipeline error:', err.message);
          Sentry.captureException(err, { tags: { callId, tenantId, phase: 'post-call' } });
        }
      });
    } catch (err) {
      console.error('[agent] Entry function error:', err.message);
      console.error('[agent] Stack:', err?.stack);
      Sentry.captureException(err);
      throw err;
    }
  },
});

// ── CLI entry point ──
cli.runApp(new ServerOptions({ agent: import.meta.filename, agentName: 'voco-voice-agent' }));
