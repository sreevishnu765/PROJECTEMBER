import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Electron loads the built app via file://, not http://. Vite's default
  // base ('/') resolves root-relative asset URLs (index.js, index.css)
  // against the filesystem root under file://, not the app folder — the
  // exact cause of the blank screen with nothing in the console. './'
  // makes every built asset reference relative to index.html instead.
  base: './',
})
