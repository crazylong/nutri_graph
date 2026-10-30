<template>
  <svg
    ref="svgRef"
    :viewBox="`0 0 ${width} ${height}`"
    class="graph"
    :class="{ dragging }"
    :height="height"
  >
    <line
      v-for="edge in edges"
      :key="edge.key"
      :x1="ends(edge).x1" :y1="ends(edge).y1"
      :x2="ends(edge).x2" :y2="ends(edge).y2"
      :stroke="color(edge.evidence_level)"
      stroke-width="1.6"
    />
    <g
      v-for="node in nodes"
      :key="node.id"
      class="node"
      @pointerdown="onPointerDown(node, $event)"
    >
      <circle :cx="node.x" :cy="node.y" :r="node.anchor ? 22 : 16" :fill="fill(node.label)" />
      <text :x="node.x" :y="node.y + 32" text-anchor="middle" class="label">{{ node.name }}</text>
    </g>
  </svg>
</template>

<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({
  subgraph: { type: Object, default: () => ({ nodes: [], edges: [] }) },
})

const width = 640
const height = 360
const fills = {
  Ingredient: '#2f7d5d',
  Nutrient: '#2a5b83',
  Population: '#b4772a',
  Symptom: '#7a52a0',
  Taboo: '#b0442f',
}
const levels = { A: '#22684c', B: '#2a5b83', C: '#b4772a', D: '#9a9a9a' }

const svgRef = ref(null)
const nodes = ref([])
const edges = ref([])
const dragging = ref(false)
let move = null
let up = null

function fill(label) {
  return fills[label] || '#6b7280'
}
function color(level) {
  return levels[level] || '#aab2ac'
}

function arrange(rawNodes, rawEdges) {
  const live = rawNodes.filter((n) => n && n.id)
  const ids = new Set(live.map((n) => n.id))
  const rels = rawEdges.filter((e) => ids.has(e.source) && ids.has(e.target))
  const anchors = live.filter((n) => n.anchor)
  const pos = new Map()
  const cx = width / 2
  const cy = height / 2
  if (anchors.length <= 1) {
    live.forEach((n, i) => {
      const angle = (Math.PI * 2 * i) / Math.max(live.length, 1)
      const ring = n.anchor ? 0 : Math.min(width, height) * 0.34
      pos.set(n.id, { x: cx + Math.cos(angle) * ring, y: cy + Math.sin(angle) * ring })
    })
  } else {
    const gap = 140
    anchors.forEach((n, i) => {
      pos.set(n.id, {
        x: cx + (i - (anchors.length - 1) / 2) * gap,
        y: cy,
      })
    })
    live.filter((n) => !n.anchor).forEach((n, i, rest) => {
      const angle = (Math.PI * 2 * i) / Math.max(rest.length, 1) - Math.PI / 2
      const ring = Math.min(width, height) * 0.36
      pos.set(n.id, { x: cx + Math.cos(angle) * ring, y: cy + Math.sin(angle) * ring })
    })
  }
  return {
    nodes: live.map((n) => ({ ...n, ...pos.get(n.id) })),
    edges: rels.map((e, i) => ({ ...e, key: `${e.source}-${e.rel || ''}-${e.target}-${i}` })),
  }
}

function relayout() {
  stopDrag()
  const laid = arrange(props.subgraph?.nodes || [], props.subgraph?.edges || [])
  nodes.value = laid.nodes
  edges.value = laid.edges
}

watch(() => props.subgraph, relayout, { immediate: true })

function radius(node) {
  return node && node.anchor ? 22 : 16
}

function ends(edge) {
  const a = nodes.value.find((n) => n.id === edge.source) || { x: 0, y: 0 }
  const b = nodes.value.find((n) => n.id === edge.target) || { x: 0, y: 0 }
  const dx = b.x - a.x
  const dy = b.y - a.y
  const dist = Math.hypot(dx, dy) || 1
  const ra = radius(a) + 1
  const rb = radius(b) + 1
  return {
    x1: a.x + (dx / dist) * ra,
    y1: a.y + (dy / dist) * ra,
    x2: b.x - (dx / dist) * rb,
    y2: b.y - (dy / dist) * rb,
  }
}

function stopDrag() {
  if (move) window.removeEventListener('pointermove', move)
  if (up) window.removeEventListener('pointerup', up)
  move = null
  up = null
  dragging.value = false
}

function viewPoint(ev, rect) {
  const scale = Math.min(rect.width / width, rect.height / height) || 1
  const offsetX = (rect.width - width * scale) / 2
  const offsetY = (rect.height - height * scale) / 2
  return {
    x: (ev.clientX - rect.left - offsetX) / scale,
    y: (ev.clientY - rect.top - offsetY) / scale,
  }
}

function onPointerDown(node, ev) {
  const svg = svgRef.value
  if (!svg) return
  ev.preventDefault()
  stopDrag()
  const rect = svg.getBoundingClientRect()
  dragging.value = true
  move = (ev2) => {
    const p = viewPoint(ev2, rect)
    node.x = Math.max(36, Math.min(width - 36, p.x))
    node.y = Math.max(28, Math.min(height - 40, p.y))
  }
  up = () => stopDrag()
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
}

onBeforeUnmount(stopDrag)
</script>

<style scoped>
.graph { width: 100%; background: #f7f8f6; border-radius: 8px; touch-action: none; user-select: none; }
.node { cursor: grab; }
.dragging .node { cursor: grabbing; }
.label { font-size: 12px; fill: #333; }
</style>
