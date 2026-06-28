import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Pin to 5173 and fail loudly if it's busy, so Vite never silently
    // steals the backend's port (5174) and breaks all API calls.
    port: 5173,
    strictPort: true,
  },
})
