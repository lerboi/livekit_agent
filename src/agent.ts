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

import { type JobContext, defineAgent, cli, llm } from '@livekit/agents';
import * as google from '@livekit/agents-plugin-google';
import { EgressClient, EncodedFileOutput, RoomServiceClient } from 'livekit-server-sdk';
import { buildSystemPrompt } from './prompt.js';
import { createTools } from './tools/index.js';
import { getSupabaseAdmin } from './supabase.js';
import { runPostCallPipeline } from './post-call.js';
import { calculateInitialSlots, type TenantRow } from './utils.js';

// Voice mapping: tone_preset → Gemini voice name
const VOICE_MAP: Record<string, string> = {
  professional: 'Kore',
  friendly: 'Aoede',
  local_expert: 'Achird',
};

export default defineAgent({
  entry: async (ctx: JobContext) => {
    await ctx.connect();

    // Wait for the SIP participant (the caller) to join
    const participant = await ctx.waitForParticipant();

    // Extract phone numbers from SIP participant attributes
    const toNumber =
      participant.attributes?.['sip.phoneNumber'] ||
      participant.attributes?.['sip.to'] ||
      '';
    const fromNumber =
      participant.attributes?.['sip.callerNumber'] ||
      participant.attributes?.['sip.from'] ||
      '';
    const callId = ctx.room.name!; // Room name = call identifier (replaces retell_call_id)

    // Check if this is a test call (metadata set by test-call route)
    let isTestCall = false;
    try {
      const roomMeta = ctx.room.metadata ? JSON.parse(ctx.room.metadata) : {};
      isTestCall = roomMeta.test_call === true;
    } catch {}

    console.log(`[agent] Call started: room=${callId} from=${fromNumber} to=${toNumber} test=${isTestCall}`);

    // ── Tenant lookup (same logic as handleInbound webhook) ──
    const supabase = getSupabaseAdmin();
    const { data: tenant } = await supabase
      .from('tenants')
      .select('*')
      .eq('phone_number', toNumber)
      .single();

    const onboardingComplete = tenant?.onboarding_complete ?? false;
    const businessName = tenant?.business_name ?? 'Voco';
    const locale = tenant?.default_locale ?? 'en';
    const tonePreset = tenant?.tone_preset ?? 'professional';
    const tenantId = tenant?.id ?? null;
    const ownerPhone = tenant?.owner_phone ?? null;
    const tenantTimezone = tenant?.tenant_timezone ?? 'America/Chicago';

    // ── Calculate available slots (same logic as handleInbound) ──
    let availableSlots = '';
    if (onboardingComplete && tenantId) {
      try {
        availableSlots = await calculateInitialSlots(supabase, tenant as TenantRow);
      } catch (err) {
        console.error('[agent] Slot calculation failed:', err);
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
          .flatMap((s: any) => s.intake_questions || [])
          .filter((q: string, i: number, arr: string[]) => arr.indexOf(q) === i)
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

    // ── Create call record immediately ──
    const startTimestamp = Date.now();
    const { data: callRecord } = await supabase
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

    const sipParticipantIdentity = participant.identity || '';

    // ── Create tools (in-process, direct Supabase access) ──
    const tools = createTools({
      supabase,
      tenant: tenant as TenantRow | null,
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

    // ── Select Gemini voice based on tone preset ──
    const voiceName = VOICE_MAP[tonePreset] || 'Kore';

    // ── Start Gemini Live session via LiveKit agent framework ──
    const model = new google.beta.realtime.RealtimeModel({
      model: 'gemini-3.1-flash-live-preview',
      voice: voiceName as any,
      temperature: 0.3,
      instructions: systemPrompt,
      inputAudioTranscription: {},
      outputAudioTranscription: {},
      contextWindowCompression: {
        slidingWindow: {},
        triggerTokens: 100000,
      },
      realtimeInputConfig: {
        automaticActivityDetection: {
          disabled: false,
          startOfSpeechSensitivity: 'START_SENSITIVITY_HIGH' as any,
          endOfSpeechSensitivity: 'END_SENSITIVITY_HIGH' as any,
          prefixPaddingMs: 100,
          silenceDurationMs: 700,
        },
        activityHandling: 'START_OF_ACTIVITY_INTERRUPTS' as any,
      },
    });

    const agent = new llm.Agent({ tools });

    const session = new llm.AgentSession({
      llm: model,
    });

    // ── Collect transcript in real-time ──
    const transcriptTurns: Array<{ role: string; content: string; timestamp: number }> = [];

    session.on('conversation_item_added', (event: any) => {
      const text = event.item?.textContent || event.item?.text;
      if (text) {
        transcriptTurns.push({
          role: event.item.role === 'user' ? 'user' : 'agent',
          content: text,
          timestamp: Date.now(),
        });
      }
    });

    // ── Start the session ──
    await session.start(ctx.room, participant);

    // ── Generate greeting ──
    await session.say('', { allowInterruptions: false });

    // ── Start Egress recording ──
    let egressId: string | undefined;
    const recordingPath = `call-recordings/${callId}.mp4`;
    try {
      const egressClient = new EgressClient(
        process.env.LIVEKIT_URL!,
        process.env.LIVEKIT_API_KEY!,
        process.env.LIVEKIT_API_SECRET!,
      );

      const s3Config = {
        accessKey: process.env.SUPABASE_S3_ACCESS_KEY!,
        secret: process.env.SUPABASE_S3_SECRET_KEY!,
        bucket: 'call-recordings',
        region: process.env.SUPABASE_S3_REGION || 'us-east-1',
        endpoint: process.env.SUPABASE_S3_ENDPOINT!,
        forcePathStyle: true,
      };

      const egressInfo = await egressClient.startRoomCompositeEgress(
        callId,
        {
          file: {
            filepath: `${callId}.mp4`,
            output: {
              case: 's3' as const,
              value: s3Config,
            },
          } as any,
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
            process.env.LIVEKIT_URL!,
            process.env.LIVEKIT_API_KEY!,
            process.env.LIVEKIT_API_SECRET!,
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
          tenant: tenant as TenantRow | null,
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
  },
});

// ── CLI entry point ──
cli.runApp(new cli.WorkerOptions({ agent: 'agent.js' }));
