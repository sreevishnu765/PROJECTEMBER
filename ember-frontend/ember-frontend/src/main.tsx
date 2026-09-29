import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { HudApp } from './hud/HudApp.tsx'

// The Electron shell loads this same bundle twice: once normally (main
// window) and once with "#hud" (the small transparent HUD window).
const isHud = window.location.hash === '#hud'
if (isHud) document.documentElement.classList.add('hud')

createRoot(document.getElementById('root')!).render(
  <StrictMode>{isHud ? <HudApp /> : <App />}</StrictMode>,
)
