import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({ gfm: true, breaks: false })

export function renderMarkdown(text) {
  const html = marked.parse(String(text || ''), { async: false })
  return DOMPurify.sanitize(html)
}
