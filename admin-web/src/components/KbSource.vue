<template>
  <el-dialog v-model="visible" :title="title" width="1040px" top="4vh" @closed="release">
    <p v-if="filename" class="file">{{ filename }}</p>
    <div v-loading="loading" class="body">
      <iframe v-if="pdfUrl" class="pdf" :src="pdfUrl" title="原文" />
      <div v-else-if="html" ref="mdRoot" class="md" v-html="html" @click="stay" />
      <pre v-else-if="text" class="text">{{ text }}</pre>
      <el-empty v-else-if="!loading" description="没有可显示的原文" :image-size="64" />
    </div>
  </el-dialog>
</template>

<script setup>
import { nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, errorMessage, readJson } from '../api'
import { renderMarkdown } from '../markdown.js'

let mermaidModule = null

function loadMermaid() {
  if (!mermaidModule) {
    mermaidModule = import('mermaid').then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        suppressErrorRendering: true,
        fontFamily: '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif',
      })
      return mermaid
    })
  }
  return mermaidModule
}

const visible = ref(false)
const loading = ref(false)
const title = ref('原文')
const filename = ref('')
const pdfUrl = ref('')
const text = ref('')
const html = ref('')
const mdRoot = ref(null)
let ticket = 0

function release() {
  if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value)
  pdfUrl.value = ''
  text.value = ''
  html.value = ''
}

function stay(event) {
  if (event.target.closest('a')) event.preventDefault()
}

function isMarkdown(name, type) {
  return name.endsWith('.md') || name.endsWith('.markdown') || type.includes('markdown')
}

async function drawMermaid(root) {
  if (!root) return
  const nodes = [...root.querySelectorAll('.mermaid')]
  if (!nodes.length) return
  const mermaid = await loadMermaid()
  for (const node of nodes) {
    const source = node.textContent
    try {
      await mermaid.run({ nodes: [node] })
    } catch {
      node.textContent = source
    }
  }
}

async function open(row) {
  const mine = ++ticket
  release()
  title.value = row.title || '原文'
  filename.value = row.filename || ''
  visible.value = true
  loading.value = true
  const name = filename.value.toLowerCase()
  const query = name.endsWith('.docx') ? '?text=1' : ''
  try {
    const response = await api('/admin/kb/docs/' + row.id + '/file' + query)
    if (mine !== ticket) return
    if (!response.ok) {
      const body = await readJson(response)
      ElMessage.error(errorMessage(body, '原文打不开'))
      visible.value = false
      return
    }
    const type = response.headers.get('content-type') || ''
    if (type.includes('pdf')) {
      const blob = new Blob([await response.arrayBuffer()], { type: 'application/pdf' })
      pdfUrl.value = URL.createObjectURL(blob)
      return
    }
    const body = await response.text()
    if (isMarkdown(name, type)) {
      html.value = renderMarkdown(body)
      await nextTick()
      if (mine !== ticket) return
      await drawMermaid(mdRoot.value)
      return
    }
    text.value = body
  } catch {
    if (mine !== ticket) return
    ElMessage.error('原文打不开')
    visible.value = false
  } finally {
    if (mine === ticket) loading.value = false
  }
}

defineExpose({ open })
</script>

<style scoped>
.file {
  margin: 0 0 8px;
  color: #909399;
  font-size: 13px;
}
.body {
  min-height: 240px;
}
.pdf {
  width: 100%;
  height: 72vh;
  border: 0;
  background: #f4f6f4;
}
.text,
.md {
  max-height: 72vh;
  overflow: auto;
}
.text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 14px;
  line-height: 1.7;
  color: #303133;
}
.md {
  font-size: 14px;
  line-height: 1.7;
  color: #303133;
}
.md :deep(> :first-child) { margin-top: 0; }
.md :deep(h1) { font-size: 22px; margin: 0 0 0.6em; }
.md :deep(h2) { font-size: 18px; margin: 1.2em 0 0.45em; }
.md :deep(h3) { font-size: 16px; margin: 1em 0 0.4em; }
.md :deep(p) { margin: 0.55em 0; }
.md :deep(ul),
.md :deep(ol) { margin: 0.4em 0; padding-left: 1.4em; }
.md :deep(a) { color: #2f7d5d; }
.md :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
}
.md :deep(:not(pre) > code) {
  background: #eef3ee;
  padding: 1px 5px;
  border-radius: 4px;
}
.md :deep(pre:not(.mermaid)) {
  margin: 0.7em 0;
  padding: 12px 14px;
  background: #f4f6f4;
  border: 1px solid #d6e8de;
  border-radius: 8px;
  overflow: auto;
  line-height: 1.5;
}
.md :deep(pre code) { background: none; padding: 0; }
.md :deep(pre.mermaid) {
  margin: 0.8em 0;
  padding: 12px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  overflow: auto;
  text-align: center;
}
.md :deep(blockquote) {
  margin: 0.7em 0;
  padding: 4px 0 4px 12px;
  border-left: 3px solid #2f7d5d;
  color: #606266;
}
.md :deep(table) {
  border-collapse: collapse;
  margin: 0.6em 0;
  display: block;
  max-width: 100%;
  overflow: auto;
}
.md :deep(th),
.md :deep(td) {
  border: 1px solid #d6e8de;
  padding: 6px 10px;
  text-align: left;
}
.md :deep(th) { background: #f4f6f4; }
</style>

<style>
.kb-title-link {
  padding: 0;
  border: 0;
  background: none;
  color: #2f7d5d;
  font: inherit;
  line-height: inherit;
  text-align: left;
  cursor: pointer;
}
.kb-title-link:hover {
  text-decoration: underline;
}
</style>
