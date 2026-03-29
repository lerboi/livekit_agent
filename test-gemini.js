/**
 * Minimal test: connect to Gemini 3.1 Flash Live directly via @google/genai
 * Run: GOOGLE_API_KEY=xxx node test-gemini.js
 */

import { GoogleGenAI, Modality } from '@google/genai';

const ai = new GoogleGenAI({ apiKey: process.env.GOOGLE_API_KEY });

async function test() {
  console.log('Connecting to gemini-3.1-flash-live-preview...');

  try {
    const session = await ai.live.connect({
      model: 'gemini-3.1-flash-live-preview',
      config: {
        responseModalities: [Modality.AUDIO],
        speechConfig: {
          voiceConfig: {
            prebuiltVoiceConfig: { voiceName: 'Kore' }
          }
        },
        systemInstruction: {
          parts: [{ text: 'You are a helpful assistant. Say hello.' }]
        },
      },
      callbacks: {
        onopen: () => console.log('Session opened!'),
        onmessage: (msg) => {
          if (msg.serverContent?.modelTurn?.parts) {
            for (const part of msg.serverContent.modelTurn.parts) {
              if (part.inlineData) {
                console.log('Received audio chunk:', part.inlineData.data.length, 'bytes');
              }
              if (part.text) {
                console.log('Received text:', part.text);
              }
            }
          }
          if (msg.setupComplete) {
            console.log('Setup complete!');
          }
        },
        onerror: (err) => console.error('Error:', err),
        onclose: (event) => console.log('Closed:', event.code, event.reason),
      },
    });

    console.log('Connected! Waiting 3s for setup...');
    await new Promise(r => setTimeout(r, 3000));

    // Send a text message to trigger a response
    console.log('Sending text input...');
    session.sendRealtimeInput({ text: 'Hello, say something brief.' });

    // Wait for response
    await new Promise(r => setTimeout(r, 5000));

    console.log('Closing session...');
    session.close();
  } catch (err) {
    console.error('Connection failed:', err.message);
    console.error('Full error:', JSON.stringify(err, null, 2));
  }
}

test();
