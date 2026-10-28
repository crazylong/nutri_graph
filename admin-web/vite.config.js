import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/console/',
  plugins: [vue()],
  server: {
    proxy: {
      '/auth': 'http://127.0.0.1:8810',
      '/admin': 'http://127.0.0.1:8810',
    },
  },
})
