<template>
  <svg :viewBox="`0 0 ${width} ${height}`" class="graph" :height="height">
    <line
      v-for="(edge, i) in placed.edges"
      :key="i"
      :x1="edge.x1" :y1="edge.y1" :x2="edge.x2" :y2="edge.y2"
      :stroke="color(edge.evidence_level)"
      stroke-width="1.6"
    />
    <g v-for="node in placed.nodes" :key="node.id">
      <circle :cx="node.x" :cy="node.y" :r="node.anchor ? 22 : 16" :fill="fill(node.label)" />
      <text :x="node.x" :y="node.y + 32" text-anchor="middle" class="label">{{ node.name }}</text>
    </g>
  </svg>
</template>

<script setup>
import { computed } from 'vue'

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

function fill(label) {
  return fills[label] || '#6b7280'
}
function color(level) {
  return levels[level] || '#aab2ac'
}

const placed = computed(() => layout(props.subgraph?.nodes || [], props.subgraph?.edges || []))

function layout(nodes, edges) {
  const live = nodes.filter((n) => n && n.id)
  const ids = new Set(live.map((n) => n.id))
  const rels = edges.filter((e) => ids.has(e.source) && ids.has(e.target))
  const pos = new Map()
  live.forEach((n, i) => {
    const angle = (Math.PI * 2 * i) / Math.max(live.length, 1)
    const ring = n.anchor ? 0 : Math.min(width, height) * 0.34
    pos.set(n.id, {
      x: width / 2 + Math.cos(angle) * ring,
      y: height / 2 + Math.sin(angle) * ring,
    })
  })
  return {
    nodes: live.map((n) => ({ ...n, ...pos.get(n.id) })),
    edges: rels.map((e) => ({
      ...e,
      x1: pos.get(e.source).x,
      y1: pos.get(e.source).y,
      x2: pos.get(e.target).x,
      y2: pos.get(e.target).y,
    })),
  }
}
</script>

<style scoped>
.graph { width: 100%; background: #f7f8f6; border-radius: 8px; }
.label { font-size: 12px; fill: #333; }
</style>
