import DOMPurify from 'dompurify'
import { marked } from 'marked'

const renderer = new marked.Renderer()

renderer.code = function ({ text, lang, escaped }) {
  const language = ((lang || '').match(/^\S*/)?.[0] || '').toLowerCase()
  if (language === 'mermaid') {
    return '<pre class="mermaid">' + escapeHtml(text) + '</pre>\n'
  }
  const body = escaped ? text : escapeHtml(text)
  const klass = language ? ' class="language-' + escapeHtml(language) + '"' : ''
  return '<pre><code' + klass + '>' + body + '</code></pre>\n'
}

marked.setOptions({ gfm: true, breaks: false, renderer })

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

export function renderMarkdown(text) {
  const html = marked.parse(String(text || ''), { async: false })
  return DOMPurify.sanitize(html)
}
