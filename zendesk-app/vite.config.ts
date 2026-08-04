import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

export default defineConfig({
  plugins: [react()],
  root: 'src',
  build: {
    outDir: resolve(__dirname, 'assets'),
    emptyOutDir: false, // don't delete index.html (managed separately)
    rollupOptions: {
      input: {
        app: resolve(__dirname, 'src/main.tsx'),
        modal: resolve(__dirname, 'src/modal.tsx'),
      },
      output: {
        entryFileNames: '[name].js',
        chunkFileNames: '[name]-[hash].js',
        assetFileNames: (info) => info.name?.endsWith('.css') ? 'app.css' : (info.name ?? 'asset'),
      },
    },
  },
})
