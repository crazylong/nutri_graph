import assert from 'node:assert/strict'
import test from 'node:test'
import { placeImages } from './answer-blocks.js'

const answer = [
  '开头这段不插图。第一章 Apache Flink概述 / 1.1.1 Flink的定义',
  '',
  '1. 第一条',
  '- 依据章节：第一章 Apache Flink概述 / 1.1.1 Flink的定义',
  '',
  '2. 第二条',
  '- 依据章节：1.2.3 数据分析-大数据Lambda架构',
  '',
  '3. 第三条',
  '- 依据章节：1.2 数据架构的演变 / 1.2.1 业务处理-单体架构',
].join('\n')

const hits = [
  { heading_path: '第一章 Apache Flink概述 / 1.1.1 Flink的定义', preview: 'blob:a' },
  { heading_path: '1.2.3 数据分析-大数据Lambda架构', preview: 'blob:b' },
  { heading_path: '1.2 数据架构的演变 / 1.2.1 业务处理-单体架构', preview: '' },
]

test('image sits after the matching item', () => {
  const blocks = placeImages(answer, hits)
  assert.equal(blocks.length, 4)
  assert.equal(blocks[0].image, '')
  assert.equal(blocks[1].image, 'blob:a')
  assert.equal(blocks[2].image, 'blob:b')
  assert.equal(blocks[3].image, '')
  assert.match(blocks[2].text, /^2\. /)
})

test('two chapters in one item keep the image in sources only', () => {
  const mixed = '1. 依据章节：甲章 和 乙章'
  const blocks = placeImages(mixed, [
    { heading_path: '甲章', preview: 'blob:a' },
    { heading_path: '乙章', preview: 'blob:b' },
  ])
  assert.equal(blocks.length, 1)
  assert.equal(blocks[0].image, '')
})

test('shorter heading inside a longer one does not steal the image', () => {
  const item = '1. 依据章节：第一章 Apache Flink概述 / 1.1.1 Flink的定义'
  const blocks = placeImages(item, [
    { heading_path: '1.1.1 Flink的定义', preview: 'blob:short' },
    { heading_path: '第一章 Apache Flink概述 / 1.1.1 Flink的定义', preview: 'blob:long' },
  ])
  assert.equal(blocks[0].image, 'blob:long')
})

test('plain answer without a numbered list has no inserted image', () => {
  const blocks = placeImages('没有编号。1.1.1 Flink的定义', hits)
  assert.equal(blocks.length, 1)
  assert.equal(blocks[0].image, '')
})

test('heading markdown and a shortened citation still place the image', () => {
  const markdown = [
    '根据文档块内容，该图可能对应以下三种场景之一。',
    '',
    '### 1. Apache Flink 统一数据处理架构',
    '若图中左侧为多种输入源。',
    '*   **依据**：`第一章 / 1.1.1 Flink的定义`',
    '',
    '### 2. Lambda 架构数据处理流程',
    '*   **依据**：`1.2.3 数据分析-大数据Lambda架构`',
    '',
    '### 3. Milvus 全文搜索处理流程',
    '*   **依据**：`d、全文索引和搜索`',
    '',
    '---',
    '',
    '**补充说明：** 以上三个选项覆盖了主要图示。',
  ].join('\n')
  const blocks = placeImages(markdown, [
    { heading_path: '1 第一章 Apache Flink概述 / 1.1.1 Flink的定义', preview: 'blob:flink' },
    { heading_path: '1.2.3 数据分析-大数据Lambda架构', preview: 'blob:lambda' },
    { heading_path: '1.2 数据架构的演变 / 1.2.1 业务处理-单体架构', preview: 'blob:micro' },
    { heading_path: 'd、全文索引和搜索', preview: 'blob:milvus' },
  ])
  assert.equal(blocks[0].image, '')
  assert.match(blocks[1].text, /^### 1\./)
  assert.equal(blocks[1].image, 'blob:flink')
  assert.equal(blocks[2].image, 'blob:lambda')
  assert.equal(blocks[3].image, 'blob:milvus')
  assert.match(blocks[4].text, /补充说明/)
  assert.equal(blocks[4].image, '')
  assert.equal(blocks.some((block) => block.image === 'blob:micro'), false)
})
