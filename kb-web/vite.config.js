import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/ask/',
  plugins: [vue()],
  server: {
    proxy: {
      '/kb': 'http://127.0.0.1:8810',
    },
  },
})
