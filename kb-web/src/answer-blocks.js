const ITEM_START = /^(?:#{1,6}[ \t]+)?(?:\*\*)?\d+\.[ \t]/m

export function placeImages(markdown, hits) {
  const text = String(markdown || '').replace(/\r\n/g, '\n')
  const found = text.match(ITEM_START)
  if (!found) return [{ text, image: '' }]
  const lead = text.slice(0, found.index).trim()
  const items = text.slice(found.index).split(/^(?=(?:#{1,6}[ \t]+)?(?:\*\*)?\d+\.[ \t])/m).map((part) => part.trim()).filter(Boolean)
  const blocks = []
  if (lead) blocks.push({ text: lead, image: '' })
  for (const item of items) {
    const [body, tail] = peelTail(item)
    blocks.push({ text: body, image: imageForItem(body, hits) })
    if (tail) blocks.push({ text: tail, image: '' })
  }
  return blocks
}

function peelTail(item) {
  const cut = item.search(/\n---\s*\n/)
  if (cut < 0) return [item, '']
  const tail = item.slice(cut).trim()
  if (tail.includes('依据')) return [item, '']
  return [item.slice(0, cut).trim(), tail]
}

function needles(heading) {
  const parts = heading.split(/\s*\/\s*/).map((part) => part.trim()).filter(Boolean)
  const leaf = parts[parts.length - 1] || ''
  const found = []
  if (heading.length >= 2) found.push(heading)
  if (leaf && leaf !== heading && leaf.length >= 4 && leaf !== '正文') found.push(leaf)
  return found
}

function imageForItem(item, hits) {
  const matched = []
  for (const hit of hits || []) {
    const heading = String(hit && hit.heading_path || '').trim()
    if (!heading || heading === '正文') continue
    const used = needles(heading).filter((needle) => item.includes(needle))
    if (!used.length) continue
    used.sort((a, b) => b.length - a.length)
    matched.push({ heading, needle: used[0], image: hit.preview || '' })
  }
  const kept = matched.filter((row) => !matched.some((other) => other.needle.length > row.needle.length && other.needle.includes(row.needle)))
  const headings = [...new Set(kept.map((row) => row.heading))]
  if (headings.length !== 1) return ''
  const images = [...new Set(kept.filter((row) => row.heading === headings[0] && row.image).map((row) => row.image))]
  return images.length === 1 ? images[0] : ''
}
