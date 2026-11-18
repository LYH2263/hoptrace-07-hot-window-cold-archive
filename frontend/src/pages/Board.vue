<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const waybills = ref([])
const selected = ref(null)
const detail = ref(null)
const overview = ref(null)
const hotcold = ref(null)
const includeCold = ref(false)
const windowInput = ref(3)
const winMsg = ref('')
const err = ref('')

async function loadList() {
  overview.value = await api.overview()
  waybills.value = await api.waybills()
  if (!selected.value && waybills.value.length) {
    selected.value = waybills.value[0].id
  }
}

async function loadDetail(id) {
  if (!id) return
  detail.value = await api.timeline(id, includeCold.value)
  hotcold.value = await api.hotcoldProof(id)
}

onMounted(async () => {
  try {
    await loadList()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(selected, async (id) => {
  winMsg.value = ''
  try {
    await loadDetail(id)
    if (hotcold.value) windowInput.value = hotcold.value.window.max_points
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(includeCold, async () => {
  if (!selected.value) return
  try {
    await loadDetail(selected.value)
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function applyWindow() {
  if (!selected.value || !windowInput.value) return
  winMsg.value = ''
  err.value = ''
  try {
    const res = await api.setHotWindow(selected.value, Number(windowInput.value))
    const cut = res.cut || {}
    winMsg.value = cut.archived
      ? `切窗完成：${cut.archived} 点入冷，段哈希 ${String(cut.seg_hash).slice(0, 12)}…`
      : '切窗完成：无溢出点'
    await loadDetail(selected.value)
    overview.value = await api.overview()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

const hops = computed(() => detail.value?.hops || [])
const proof = computed(() => detail.value?.proof)
</script>

<template>
  <div>
    <p v-if="err" class="muted">{{ err }}</p>
    <div class="stat-row" v-if="overview">
      <div class="stat"><b>{{ overview.waybills }}</b><span>运单</span></div>
      <div class="stat"><b>{{ overview.hops_primary }}</b><span>正式 hop</span></div>
      <div class="stat"><b>{{ overview.hops_cold }}</b><span>冷归档</span></div>
      <div class="stat"><b>{{ overview.hops_sidecar }}</b><span>旁路</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
    </div>

    <div class="tri">
      <aside class="pane waybill-pane">
        <h2>运单索引</h2>
        <button
          v-for="w in waybills"
          :key="w.id"
          class="wb-item"
          :class="{ active: selected === w.id }"
          @click="selected = w.id"
        >
          <span class="id">{{ w.id }}</span>
          <span class="title">{{ w.title }}</span>
          <span class="st">{{ w.status_proj }}</span>
        </button>
      </aside>

      <section class="pane axis-pane">
        <div class="axis-head">
          <h1>{{ detail?.waybill?.title || '时间线' }}</h1>
          <span>{{ selected }}</span>
        </div>
        <div class="axis-sub" v-if="hotcold">
          <span>热段 {{ hotcold.hot_count }} 点 · 冷归档 {{ hotcold.cold_count }} 点</span>
          <label class="cold-toggle">
            <input type="checkbox" v-model="includeCold" /> 含冷段
          </label>
        </div>
        <div class="v-axis" v-if="hops.length">
          <div class="hop-node" v-for="h in hops" :key="h.id" :class="{ cold: !!h.is_cold }">
            <div class="ts">{{ h.event_ts }}</div>
            <div class="code">
              {{ h.event_code }}
              <span v-if="h.is_cold" class="badge-cold">冷</span>
            </div>
            <div class="loc">{{ h.location_code }} · {{ h.id }}</div>
          </div>
        </div>
        <p v-else class="muted">无正式轨迹点</p>
      </section>

      <aside class="pane inspect-pane">
        <h2>检查器</h2>
        <dl class="kv" v-if="detail?.waybill">
          <dt>运单</dt><dd>{{ detail.waybill.id }}</dd>
          <dt>投影状态</dt><dd>{{ detail.waybill.status_proj }}</dd>
          <dt>创建</dt><dd>{{ detail.waybill.created_at }}</dd>
        </dl>

        <h2>冷热分层</h2>
        <template v-if="hotcold">
          <dl class="kv">
            <dt>热窗口</dt>
            <dd>≤ {{ hotcold.window.max_points }} 点（{{ hotcold.window.source === 'waybill' ? '运单配置' : '默认' }}）</dd>
            <dt>热点数</dt><dd>{{ hotcold.hot_count }}</dd>
            <dt>冷点数</dt><dd>{{ hotcold.cold_count }}</dd>
            <dt>冷段哈希</dt><dd>{{ hotcold.cold_hash || '—' }}</dd>
            <dt>一致性</dt>
            <dd :class="hotcold.consistent ? 'ok-text' : 'bad-text'">
              {{ hotcold.consistent ? '账实相符' : '账实不符' }}
            </dd>
          </dl>
          <div class="win-ctl">
            <input v-model.number="windowInput" type="number" min="1" max="10000" />
            <button class="btn" @click="applyWindow">调窗切窗</button>
          </div>
          <p v-if="winMsg" class="muted small">{{ winMsg }}</p>
          <ul class="seg-list" v-if="hotcold.segments.length">
            <li v-for="s in hotcold.segments" :key="s.seg_seq">
              冷段 #{{ s.seg_seq }} · {{ s.point_count }} 点 · {{ s.point_ids.join(', ') }}<br />
              {{ s.seg_hash }}
            </li>
          </ul>
        </template>
        <p v-else class="muted">暂无冷热数据</p>

        <h2>状态证明</h2>
        <dl class="kv" v-if="proof">
          <dt>末 hop</dt><dd>{{ proof.last_hop_id }}</dd>
          <dt>映射码</dt><dd>{{ proof.mapped_code }}</dd>
          <dt>投影值</dt><dd>{{ proof.proj_value }}</dd>
          <dt>写入</dt><dd>{{ proof.created_at }}</dd>
        </dl>
        <p v-else class="muted">暂无证明</p>
      </aside>

      <footer class="pane dot-rail">
        <span class="label">HOP</span>
        <template v-for="(h, i) in hops" :key="h.id">
          <span class="dot" :class="{ on: !h.is_cold, cold: !!h.is_cold }"></span>
          <span v-if="i < hops.length - 1" class="dot-line"></span>
        </template>
        <template v-if="!hops.length">
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
        </template>
      </footer>
    </div>
  </div>
</template>
