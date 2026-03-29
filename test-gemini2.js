/**
 * Test 2: Connect with the same config the LiveKit plugin sends
 */

import { GoogleGenAI, Modality } from '@google/genai';

const ai = new GoogleGenAI({ apiKey: process.env.GOOGLE_API_KEY });

async function test() {
  console.log('Test: connect with full LiveKit-style config...');

  const config = {
    responseModalities: [Modality.AUDIO],
    systemInstruction: {
      parts: [{ text: 'You are a helpful assistant. Say hello.' }]
    },
    speechConfig: {
      voiceConfig: {
        prebuiltVoiceConfig: { voiceName: 'Kore' }
      }
    },
    tools: [
      {
        functionDeclarations: [
          {
            name: 'transfer_call',
            description: 'Transfer the call',
            parameters: {
              type: 'object',
              properties: {
                caller_name: { type: 'string', description: 'Name' },
              },
            },
          },
        ],
      },
    ],
    temperature: 0.3,
  };

  console.log('Config:', JSON.stringify(config, null, 2));

  try {
    const session = await ai.live.connect({
      model: 'gemini-3.1-flash-live-preview',
      config,
      callbacks: {
        onopen: () => console.log('Session opened!'),
        onmessage: (msg) => {
          if (msg.setupComplete) console.log('Setup complete!');
        },
        onerror: (err) => console.error('Error:', err),
        onclose: (event) => console.log('Closed:', event.code, event.reason),
      },
    });

    await new Promise(r => setTimeout(r, 3000));
    console.log('Success! Closing...');
    session.close();
  } catch (err) {
    console.error('Failed:', err.message);
  }
}

test();
