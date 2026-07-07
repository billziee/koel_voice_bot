import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteStaticCopy } from 'vite-plugin-static-copy'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    react(),
    // Silero VAD runtime assets (see src/lib/vad.ts). The worklet + ONNX model +
    // onnxruntime WASM must be served as plain static files at the site root —
    // they are fetched by path at runtime, not imported through the bundler.
    viteStaticCopy({
      targets: [
        { src: 'node_modules/@ricky0123/vad-web/dist/silero_vad_v5.onnx', dest: '.' },
        { src: 'node_modules/@ricky0123/vad-web/dist/vad.worklet.bundle.min.js', dest: '.' },
        { src: 'node_modules/onnxruntime-web/dist/ort-wasm-simd-threaded*', dest: '.' },
      ],
    }),
  ],
  server: {
    port: process.env.PORT ? Number(process.env.PORT) : 5173,
  },
})
