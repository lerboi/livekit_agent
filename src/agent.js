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

import { defineAgent, cli, ServerOptions, voice } from '@livekit/agents';
import * as google from '@livekit/agents-plugin-google';
import { EgressClient, RoomServiceClient } from 'livekit-server-sdk';
import { buildSystemPrompt } from './prompt.js';
import { createTools } from './tools/index.js';
import { getSupabaseAdmin } from './supabase.js';
import { runPostCallPipeline } from './post-call.js';
import { calculateInitialSlots } from './utils.js';

// Voice mapping: tone_preset → Gemini voice name
const VOICE_MAP = {
  professional: 'Kore',
  friendly: 'Aoede',
  local_expert: 'Achird',
};

export default defineAgent({
  entry: async (ctx) => {
    try {
    // ── Phase 1: Connect to room ──
    console.log('[agent] Phase 1: Connecting to room...');
    await ctx.connect();
    console.log('[agent] Phase 1: Connected. Room:', ctx.room.name);

    // ── Phase 2: Wait for SIP participant ──
    console.log('[agent] Phase 2: Waiting for SIP participant...');
    const participant = await ctx.waitForParticipant();
    console.log('[agent] Phase 2: Participant joined:', participant.identity);

    // Extract phone numbers from SIP participant attributes
    console.log('[agent] Participant attributes:', JSON.stringify(participant.attributes));
    const toNumber =
      participant.attributes?.['sip.phoneNumber'] ||
      participant.attributes?.['sip.to'] ||
      '';
    const fromNumber =
      participant.attributes?.['sip.callerNumber'] ||
      participant.attributes?.['sip.from'] ||
      '';
    const callId = ctx.room.name; // Room name = call identifier

    // Check if this is a test call (metadata set by test-call route)
    let isTestCall = false;
    try {
      const roomMeta = ctx.room.metadata ? JSON.parse(ctx.room.metadata) : {};
      isTestCall = roomMeta.test_call === true;
    } catch {}

    console.log(`[agent] Call started: room=${callId} from=${fromNumber} to=${toNumber} test=${isTestCall}`);

    // ── Phase 3: Supabase + tenant lookup ──
    console.log('[agent] Phase 3: Initializing Supabase...');
    const supabase = getSupabaseAdmin();
    console.log('[agent] Phase 3: Supabase client created. Looking up tenant by phone_number:', toNumber);
    const { data: tenant, error: tenantError } = await supabase
      .from('tenants')
      .select('*')
      .eq('phone_number', toNumber)
      .single();

    if (tenantError) {
      console.error('[agent] Phase 3: Tenant lookup error:', tenantError.message, tenantError.code);
    }
    console.log('[agent] Phase 3: Tenant found:', tenant?.id ?? 'NONE', tenant?.business_name ?? 'N/A');

    const onboardingComplete = tenant?.onboarding_complete ?? false;
    const businessName = tenant?.business_name ?? 'Voco';
    const locale = tenant?.default_locale ?? 'en';
    const tonePreset = tenant?.tone_preset ?? 'professional';
    const tenantId = tenant?.id ?? null;
    const ownerPhone = tenant?.owner_phone ?? null;
    const tenantTimezone = tenant?.tenant_timezone ?? 'America/Chicago';

    // ── Phase 4: Calculate available slots ──
    let availableSlots = '';
    if (onboardingComplete && tenantId) {
      try {
        console.log('[agent] Phase 4: Calculating initial slots...');
        availableSlots = await calculateInitialSlots(supabase, tenant);
        console.log('[agent] Phase 4: Slots calculated:', availableSlots ? 'yes' : 'none');
      } catch (err) {
        console.error('[agent] Phase 4: Slot calculation failed:', err);
      }
    } else {
      console.log('[agent] Phase 4: Skipping slots (onboarding_complete:', onboardingComplete, 'tenantId:', tenantId, ')');
    }

    // ── Phase 5: Fetch intake questions ──
    let intakeQuestions = '';
    if (tenantId) {
      console.log('[agent] Phase 5: Fetching intake questions...');
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
      console.log('[agent] Phase 5: Intake questions:', intakeQuestions ? 'yes' : 'none');
    }

    // ── Phase 6: Build system prompt ──
    console.log('[agent] Phase 6: Building system prompt...');
    let systemPrompt = buildSystemPrompt(locale, {
      business_name: businessName,
      onboarding_complete: onboardingComplete,
      tone_preset: tonePreset,
      intake_questions: intakeQuestions,
    });
    if (availableSlots) {
      systemPrompt += `\n\nAVAILABLE APPOINTMENT SLOTS:\n${availableSlots}`;
    }
    console.log('[agent] Phase 6: Prompt built. Length:', systemPrompt.length);

    // ── Phase 7: Create call record ──
    console.log('[agent] Phase 7: Creating call record...');
    const startTimestamp = Date.now();
    const { data: callRecord, error: callError } = await supabase
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
      console.error('[agent] Phase 7: Call record error:', callError.message, callError.code);
    }
    console.log('[agent] Phase 7: Call record:', callRecord?.id ?? 'FAILED');

    const sipParticipantIdentity = participant.identity || '';

    // ── Phase 8: Create tools ──
    console.log('[agent] Phase 8: Creating tools...');
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
    console.log('[agent] Phase 8: Tools created:', Object.keys(tools).join(', '));

    // ── Phase 9: Create Gemini model + agent + session ──
    const voiceName = VOICE_MAP[tonePreset] || 'Kore';
    console.log('[agent] Phase 9: Creating Gemini RealtimeModel (voice:', voiceName, ')...');

    const model = new google.beta.realtime.RealtimeModel({
      model: 'gemini-3.1-flash-live-preview',
      voice: voiceName,
      temperature: 0.3,
      instructions: systemPrompt,
      inputAudioTranscription: {},
      outputAudioTranscription: {},
    });
    console.log('[agent] Phase 9: RealtimeModel created');

    const agent = new voice.Agent({
      instructions: systemPrompt,
      tools: Object.values(tools),
    });
    console.log('[agent] Phase 9: Agent created');

    const session = new voice.AgentSession({
      llm: model,
    });
    console.log('[agent] Phase 9: AgentSession created');

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

    // ── Phase 10: Start session ──
    console.log('[agent] Phase 10: Starting session...');
    await session.start({ agent, room: ctx.room });
    console.log('[agent] Phase 10: Session started');

    // ── Phase 11: Generate greeting ──
    // ── Phase 11: Generate greeting ──
    console.log('[agent] Phase 11: Generating greeting...');
    session.generateReply();
    console.log('[agent] Phase 11: Greeting triggered');

    // ── Start Egress recording ──
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
      await supabase.from('calls').update({ egress_id: egressId }).eq('call_id', callId);
    } catch (err) {
      console.error('[agent] Failed to start egress:', err);
    }

    // ── Handle session end (post-call pipeline) ──
    session.on('close', async () => {
      const endTimestamp = Date.now();
      console.log(`[agent] Session closed: room=${callId} duration=${Math.round((endTimestamp - startTimestamp) / 1000)}s`);

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
          console.error('[agent] Failed to stop egress:', err);
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
        console.error('[agent] Post-call pipeline error:', err);
      }
    });
    } catch (err) {
      console.error('[agent] ENTRY FUNCTION ERROR:', err);
      console.error('[agent] Error stack:', err?.stack);
      throw err;
    }
  },
});

// ── CLI entry point ──
cli.runApp(new ServerOptions({ agent: import.meta.filename, agentName: 'voco-voice-agent' }));
