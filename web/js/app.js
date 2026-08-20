/* ============================================================
   中加贸易智能分析 —— 前端应用（Vue3 + Element Plus + ECharts + Iconfont）
   纯浏览器端全局构建版：无打包工具，随 trade_monitor.py 静态托管
   ============================================================ */
(function () {
  'use strict';

  const { createApp, ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } = Vue;
  const ElMessage = ElementPlus.ElMessage;

  /* ---------------- 通用工具 ---------------- */
  const fmt = n => Number(n || 0).toLocaleString('zh-CN');
  const fmtMoney = n => '¥' + fmt(n);
  const fmtDollar = n => '$' + fmt(n);
  // 双精度浮点数呈现：整数补一位小数（1 → 1.0），已有小数保留原值
  function fmtDouble(v) {
    if (v === '' || v === null || v === undefined) return v;
    const n = parseFloat(v);
    if (isNaN(n)) return v;
    return Number.isInteger(n) ? n.toFixed(1) : String(n);
  }
  function escAttr(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  }
  // 简易 Markdown 渲染（标题/列表/引用/加粗）
  function md2html(s) {
    const esc = String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
    const out = [];
    let inUl = false;
    for (const line of esc.split('\n')) {
      const l = line.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
      if (/^#{1,3} /.test(l)) {
        if (inUl) { out.push('</ul>'); inUl = false; }
        out.push('<h3>' + l.replace(/^#+ /, '') + '</h3>');
      } else if (/^\s*- /.test(l)) {
        if (!inUl) { out.push('<ul>'); inUl = true; }
        out.push('<li>' + l.replace(/^\s*- /, '') + '</li>');
      } else if (/^> /.test(l)) {
        if (inUl) { out.push('</ul>'); inUl = false; }
        out.push('<blockquote>' + l.slice(2) + '</blockquote>');
      } else if (l.trim() === '') {
        if (inUl) { out.push('</ul>'); inUl = false; }
      } else {
        if (inUl) { out.push('</ul>'); inUl = false; }
        out.push('<p>' + l + '</p>');
      }
    }
    if (inUl) out.push('</ul>');
    return out.join('');
  }
  async function api(url, options) {
    const res = await fetch(url, options);
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  /* ============================================================
     视图组件
     ============================================================ */

  /* ---------- 概览 ---------- */
  const OverviewView = {
    props: { summary: Object },
    data() {
      return { fetching: false, msg: '' };
    },
    computed: {
      r() { return this.summary.latest_rate || null; },
      cards() {
        const r = this.r;
        return [
          ['USD / CNY', r ? r.usd_cny.toFixed(4) : '--', '美元兑人民币'],
          ['USD / GHS', r ? r.usd_ghs.toFixed(4) : '--', '美元兑加纳塞地'],
          ['GHS / CNY', r ? r.ghs_cny.toFixed(4) : '--', '塞地兑人民币（交叉）'],
          ['塞地30日变动', this.summary.ghs_change_pct !== null && this.summary.ghs_change_pct !== undefined
            ? this.summary.ghs_change_pct + '%' : '待积累', 'USD/GHS 升 = 塞地贬值'],
        ];
      },
      topPolicies() { return (this.summary.policies || []).slice(0, 8); },
      reportBrief() {
        const lr = this.summary.last_report;
        if (!lr) return '';
        const txt = String(lr.content || '').replace(/[#*>`]/g, '').replace(/\n+/g, ' ');
        return txt.length > 220 ? txt.slice(0, 220) + '…' : txt;
      },
    },
    methods: {
      async runFetch() {
        this.fetching = true; this.msg = '采集中，请稍候…';
        try {
          const d = await api('/api/fetch', { method: 'POST' });
          this.msg = '汇率: ' + (d.rate_ok ? '成功' : '失败') + ' ｜ 政策源成功 ' + d.policy_ok + '/' + d.policy_total
            + ' ｜ 配件词成功 ' + (d.parts_ok !== undefined ? d.parts_ok + '/' + d.parts_total : '-');
          this.$emit('refresh');
        } catch (e) {
          this.msg = '采集失败: ' + e.message;
        } finally { this.fetching = false; }
      },
    },
    template: `
    <div class="content-inner">
      <div class="section-head">
        <div class="kicker">Trade Monitor</div>
        <h2>概览</h2>
        <div class="desc">中加（纳）贸易智能分析 —— 汇率、政策、配件报价与利润测算一站式监控。</div>
      </div>
      <div class="grid-cards">
        <div class="card" v-for="c in cards" :key="c[0]">
          <div class="label">{{ c[0] }}</div>
          <div class="value">{{ c[1] }}</div>
          <div class="note">{{ c[2] }}</div>
        </div>
      </div>
      <div class="toolbar">
        <el-button type="primary" :loading="fetching" @click="runFetch">
          <span class="iconfont icon-arrow-clockwise" style="margin-right:6px"></span>立即采集一次
        </el-button>
        <span v-if="msg" style="color:#86868b;font-size:12.5px">{{ msg }}</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px" class="ov-grid">
        <div class="panel">
          <h3 class="panel-title"><span class="iconfont icon-newspaper" style="color:#0071e3"></span>政策快览
            <span class="tip">最新 {{ topPolicies.length }} 条</span></h3>
          <div v-if="topPolicies.length">
            <div v-for="p in topPolicies" :key="p.id" style="padding:7px 0;border-bottom:0.5px solid #f0f0f2">
              <div style="display:flex;justify-content:space-between;gap:10px">
                <a :href="p.url" target="_blank" rel="noopener"
                   style="color:#1d1d1f;text-decoration:none;line-height:1.5" :title="p.title">{{ p.title }}</a>
              </div>
              <div style="font-size:12px;color:#86868b;margin-top:2px">{{ p.source }}
                <span v-if="p.keyword" class="tag">{{ p.keyword.split(',')[0] }}</span>
                <span v-if="p.is_new" class="tag new">今日新增</span>
              </div>
            </div>
          </div>
          <div v-else style="color:#86868b;padding:12px 0">暂无政策数据，请点击「立即采集一次」</div>
        </div>
        <div class="panel">
          <h3 class="panel-title"><span class="iconfont icon-robot" style="color:#0071e3"></span>AI 分析日报
            <span class="tip">{{ summary.last_report ? summary.last_report.ts : '暂无' }}</span></h3>
          <div v-if="reportBrief" class="md" style="color:#424245;line-height:1.8">{{ reportBrief }}</div>
          <div v-else style="color:#86868b;padding:12px 0">
            {{ summary.ai_enabled ? '尚未生成日报，可进入「AI 日报」生成。' : '未配置 API Key，当前为规则引擎模式。' }}
          </div>
          <div v-if="summary.last_report" style="margin-top:10px;font-size:12px;color:#86868b">
            引擎: {{ summary.last_report.engine }}<span v-if="!summary.ai_enabled">（未配置API Key，规则引擎模式）</span>
          </div>
        </div>
      </div>
    </div>`,
  };

  /* ---------- 汇率趋势 ---------- */
  const RatesView = {
    props: { rates: Array, latest: Object },
    data() { return { chart: null }; },
    computed: {
      hasData() { return (this.rates || []).length > 0; },
      dates() { return (this.rates || []).map(x => x.date); },
    },
    methods: {
      render() {
        if (!this.chart || !this.hasData) return;
        this.chart.setOption({
          tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross', label: { backgroundColor: '#3a3a3c' } },
            backgroundColor: 'rgba(29,29,31,0.88)',
            borderWidth: 0,
            padding: [10, 14],
            textStyle: { color: '#fff', fontSize: 12 },
            formatter(params) {
              const p = Array.isArray(params) ? params : [params];
              const date = (p[0] && p[0].axisValueLabel) || '';
              const byName = {};
              (p || []).forEach(x => { if (x && x.seriesName != null) byName[x.seriesName] = x.value; });
              const cny = byName['USD/CNY'];
              const ghs = byName['USD/GHS'];
              const fmt = v => (v === null || v === undefined || isNaN(Number(v)))
                ? '--' : Number(v).toFixed(4);
              const cross = (cny != null && ghs != null && Number(ghs) !== 0)
                ? (Number(cny) / Number(ghs)).toFixed(4) : '--';
              const dot = color => '<span style="display:inline-block;width:8px;height:8px;' +
                'border-radius:50%;background:' + color + ';margin-right:6px;' +
                'vertical-align:middle"></span>';
              const row = (name, val, color) => '<div style="display:flex;justify-content:space-between;' +
                'align-items:center;gap:20px;margin-top:5px">' +
                '<span style="display:inline-flex;align-items:center">' + dot(color) + name + '</span>' +
                '<span style="font-weight:600;font-variant-numeric:tabular-nums">' + val + '</span></div>';
              return '<div style="font-weight:600">' + date + '</div>' +
                     row('USD / CNY', fmt(cny), '#ff3b30') +
                     row('USD / GHS', fmt(ghs), '#0071e3') +
                     row('GHS / CNY 交叉', cross, '#34c759');
            },
          },
          legend: { data: ['USD/CNY', 'USD/GHS'], textStyle: { color: '#86868b' }, top: 4 },
          grid: { left: 52, right: 52, top: 44, bottom: 30 },
          xAxis: { type: 'category', data: this.dates, axisLine: { lineStyle: { color: '#d2d2d7' } },
                   axisLabel: { color: '#86868b' } },
          yAxis: [
            { type: 'value', name: '兑CNY', nameTextStyle: { color: '#86868b' },
              axisLabel: { color: '#86868b' }, splitLine: { lineStyle: { color: '#e8e8ed' } } },
            { type: 'value', name: '兑GHS', nameTextStyle: { color: '#86868b' },
              axisLabel: { color: '#86868b' }, splitLine: { show: false } },
          ],
          series: [
            { name: 'USD/CNY', type: 'line', smooth: true, data: (this.rates || []).map(x => x.usd_cny),
              symbolSize: 6, itemStyle: { color: '#ff3b30' }, lineStyle: { width: 2.5 } },
            { name: 'USD/GHS', type: 'line', smooth: true, yAxisIndex: 1,
              symbolSize: 6, data: (this.rates || []).map(x => x.usd_ghs),
              itemStyle: { color: '#0071e3' }, lineStyle: { width: 2.5 } },
          ],
        });
      },
      onResize() { if (this.chart) this.chart.resize(); },
    },
    mounted() {
      nextTick(() => {
        this.chart = echarts.init(this.$refs.chartBox);
        this.render();
      });
      window.addEventListener('resize', this.onResize);
    },
    beforeUnmount() {
      window.removeEventListener('resize', this.onResize);
      if (this.chart) { this.chart.dispose(); this.chart = null; }
    },
    watch: { rates() { this.$nextTick(() => this.render()); } },
    template: `
    <div class="content-inner">
      <div class="section-head">
        <div class="kicker">Exchange Rates</div>
        <h2>汇率趋势</h2>
        <div class="desc">USD/CNY 与 USD/GHS 历史走势（数据源 open.er-api.com，每日自动采集）。</div>
      </div>
      <div class="grid-cards">
        <div class="card">
          <div class="label">最新 USD / CNY</div>
          <div class="value">{{ latest ? latest.usd_cny.toFixed(4) : '--' }}</div>
        </div>
        <div class="card">
          <div class="label">最新 USD / GHS</div>
          <div class="value">{{ latest ? latest.usd_ghs.toFixed(4) : '--' }}</div>
        </div>
        <div class="card">
          <div class="label">数据天数</div>
          <div class="value">{{ (rates || []).length }}</div>
          <div class="note">不足 30 天时 AI 研判会如实说明</div>
        </div>
      </div>
      <div class="panel">
        <div ref="chartBox" style="height:420px"></div>
        <div v-if="!hasData" style="color:#86868b;text-align:center;padding:30px">暂无汇率数据，请先采集</div>
      </div>
    </div>`,
  };

  /* ---------- 政策监控 ---------- */
  const PoliciesView = {
    props: { policies: Array },
    data() { return { kw: '' }; },
    computed: {
      filtered() {
        const k = this.kw.trim().toLowerCase();
        if (!k) return this.policies || [];
        return (this.policies || []).filter(p =>
          (p.title || '').toLowerCase().includes(k) ||
          (p.source || '').toLowerCase().includes(k) ||
          (p.keyword || '').toLowerCase().includes(k));
      },
      todayCount() { return (this.policies || []).filter(p => p.is_new).length; },
    },
    template: `
    <div class="content-inner">
      <div class="section-head">
        <div class="kicker">Policy Monitor</div>
        <h2>政策监控</h2>
        <div class="desc">商务部 / 中国政府网 / 外汇管理局 / 重庆人社局 / 深圳人社局 / 中非合作论坛 官网要闻，
          按「出口、外汇、创业、关税、非洲」等关键词自动过滤。</div>
      </div>
      <div class="toolbar">
        <el-input v-model="kw" placeholder="搜索标题 / 来源 / 关键词" clearable style="width:280px">
          <template #prefix><span class="iconfont icon-search"></span></template>
        </el-input>
        <span style="color:#86868b;font-size:12.5px">
          共 {{ filtered.length }} 条<span v-if="todayCount"> ｜ 今日新增 {{ todayCount }} 条</span>
        </span>
      </div>
      <div class="panel" style="padding:8px 12px">
        <el-table :data="filtered" size="default" style="width:100%">
          <el-table-column prop="source" label="来源" width="130">
            <template #default="{ row }">
              <span style="font-weight:600;color:#424245">{{ row.source }}</span>
            </template>
          </el-table-column>
          <el-table-column label="标题" min-width="420">
            <template #default="{ row }">
              <a :href="row.url" target="_blank" rel="noopener" :title="row.title"
                 style="color:#1d1d1f;text-decoration:none">{{ row.title }}</a>
              <span v-if="row.keyword" class="tag">{{ row.keyword.split(',')[0] }}</span>
              <span v-if="row.is_new" class="tag new">今日新增</span>
            </template>
          </el-table-column>
          <el-table-column label="收录时间" width="120">
            <template #default="{ row }">
              <span style="color:#86868b;font-size:12.5px">{{ String(row.first_seen).slice(0, 10) }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!filtered.length" style="color:#86868b;padding:20px;text-align:center">
          暂无数据，请在概览页点击「立即采集一次」
        </div>
      </div>
    </div>`,
  };

  /* ---------- 配件报价 ---------- */
  const PartsView = {
    data() {
      return {
        kwFilter: null,          // 当前关键词筛选
        confirmed: true,         // 仅看 AI 确认
        sortDir: 'asc',
        page: 1,
        pageSize: 10,
        loading: false,
        data: null,              // 接口缓存
        fetchKw: '',             // 抓取关键词输入
        fetchMsg: '',
        fetching: false,
      };
    },
    computed: {
      stats() { return (this.data && this.data.stats) || []; },
      allQuotes() {
        const arr = [...((this.data && this.data.quotes) || [])];
        arr.sort((a, b) => this.sortDir === 'asc' ? a.price - b.price : b.price - a.price);
        return arr;
      },
      pages() { return Math.max(1, Math.ceil(this.allQuotes.length / this.pageSize)); },
      pageItems() {
        const start = (this.page - 1) * this.pageSize;
        return this.allQuotes.slice(start, start + this.pageSize);
      },
    },
    methods: {
      async load(keepPage) {
        this.loading = true;
        const prevPage = this.page;   // 记住当前页码（纠正后刷新时保持）
        try {
          const params = new URLSearchParams();
          if (this.kwFilter) params.set('keyword', this.kwFilter);
          if (this.confirmed) params.set('confirmed', '1');
          const url = '/api/parts' + (params.toString() ? '?' + params.toString() : '');
          this.data = await api(url);
          // 纠正后刷新：保持在原页；若数据变少则收敛到最后一页
          const totalPages = Math.max(1, Math.ceil(((this.data && this.data.quotes) || []).length / this.pageSize));
          this.page = keepPage ? Math.min(prevPage, totalPages) : 1;
        } catch (e) { ElMessage.error('加载配件报价失败: ' + e.message); }
        finally { this.loading = false; }
      },
      filterKw(kw) { this.kwFilter = kw; this.load(); },
      sort(dir) { this.sortDir = dir; },
      go(delta) {
        this.page += delta;
        if (this.page < 1) this.page = 1;
        if (this.page > this.pages) this.page = this.pages;
      },
      async fetchOne() {
        const kw = this.fetchKw.trim();
        if (!kw) { ElMessage.warning('请先输入关键词'); return; }
        this.fetching = true; this.fetchMsg = '抓取中，约 5–15 秒…';
        try {
          const d = await api('/api/parts/fetch', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keyword: kw }),
          });
          this.fetchMsg = d.ok ? '✔ ' + d.msg : '✘ ' + d.msg;
          this.kwFilter = d.keyword;
          await this.load();
        } catch (e) { this.fetchMsg = '抓取失败: ' + e.message; }
        finally { this.fetching = false; }
      },
      async fetchAll() {
        this.fetching = true; this.fetchMsg = '抓取全部默认关键词中，约 30–60 秒…';
        try {
          const d = await api('/api/parts/fetch', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
          });
          this.fetchMsg = '✔ ' + d.msg;
          await this.load();
        } catch (e) { this.fetchMsg = '抓取失败: ' + e.message; }
        finally { this.fetching = false; }
      },
      aiBadge(q) {
        if (q.img_ok === 1) return { cls: 'ai-ok', txt: 'AI ✓ ' + (Number(q.img_conf || 0) * 100).toFixed(1) + '%' };
        if (q.img_ok === 0) return { cls: 'ai-bad', txt: 'AI ✗' };
        return { cls: 'ai-na', txt: 'AI -' };
      },
      async reviewImg(row, label) {
        const action = label === 1 ? '确认为该商品' : '标记为非该商品';
        try {
          const d = await api('/api/vision/review', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ parts_id: row.id, label }),
          });
          if (d.skipped) { ElMessage.info(d.msg); return; }
          row.img_ok = d.new_img_ok;
          ElMessage.success(action + '成功，' + d.msg + '，后续抓取将参考该样本');
          this.load(true);   // 保持当前页刷新（标记"其实不是"后该行会消失，页码自动收敛）
        } catch (e) { ElMessage.error(action + '失败: ' + e.message); }
      },
    },
    mounted() { this.load(); },
    template: `
    <div class="content-inner">
      <div class="section-head">
        <div class="kicker">Parts Quotes</div>
        <h2>配件报价</h2>
        <div class="desc">百度爱采购批发报价监控；AI 图像校验（CLIP）通过的商品标注 ✓。每次抓取的图片都会存入样本库；发现误判点"纠正"，系统会从你的纠正中学习，下次自动修正。</div>
      </div>
      <div class="toolbar">
        <el-input v-model="fetchKw" placeholder="输入关键词抓取报价，如：摩托车离合器片" clearable style="width:300px"
                  @keyup.enter="fetchOne">
          <template #prefix><span class="iconfont icon-search"></span></template>
        </el-input>
        <el-button type="primary" :loading="fetching" @click="fetchOne">
          <span class="iconfont icon-box-seam" style="margin-right:6px"></span>抓取单个
        </el-button>
        <el-button :loading="fetching" @click="fetchAll">
          <span class="iconfont icon-arrow-clockwise" style="margin-right:6px"></span>抓取全部默认词
        </el-button>
        <el-switch v-model="confirmed" active-text="仅看 AI 确认" @change="load()" style="margin-left:4px" />
        <span v-if="fetchMsg" style="color:#86868b;font-size:12.5px">{{ fetchMsg }}</span>
      </div>
      <div class="grid-cards">
        <div class="card" style="cursor:pointer" v-for="s in stats" :key="s.keyword" @click="filterKw(s.keyword)">
          <div class="label">{{ s.keyword }}</div>
          <div class="value" style="font-size:18px">¥{{ Number(s.min_price).toFixed(2) }}<span style="font-size:12px;color:#86868b"> 起</span></div>
          <div class="note">均价 ¥{{ s.avg_price }} ｜ {{ s.n }} 条</div>
        </div>
        <div v-if="!stats.length" class="card" style="grid-column:1/-1;color:#86868b">暂无报价数据，点击上方按钮抓取</div>
      </div>
      <div class="toolbar" v-if="stats.length">
        <span style="font-size:12px;color:#86868b">筛选：</span>
        <a class="link-plain" :class="{ active: !kwFilter }" @click="filterKw(null)">全部</a>
        <a class="link-plain" v-for="s in stats" :key="s.keyword"
           :class="{ active: kwFilter === s.keyword }" @click="filterKw(s.keyword)">{{ s.keyword }}</a>
        <div style="flex:1"></div>
        <a class="link-plain" :class="{ active: sortDir === 'asc' }" @click="sort('asc')">单价 ↑</a>
        <a class="link-plain" :class="{ active: sortDir === 'desc' }" @click="sort('desc')">单价 ↓</a>
      </div>
      <div class="panel" style="padding:8px 12px">
        <el-table :data="pageItems" v-loading="loading" style="width:100%">
          <el-table-column label="商品图" width="72">
            <template #default="{ row }">
              <a v-if="row.img" :href="row.url || row.img" target="_blank" rel="noopener" title="点击查看商品大图/页面">
                <img class="parts-img" :src="row.img" alt="商品图" loading="lazy">
              </a>
              <span v-else style="color:#ccc">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="keyword" label="关键词" width="120">
            <template #default="{ row }"><span style="color:#424245">{{ row.keyword }}</span></template>
          </el-table-column>
          <el-table-column label="标题" min-width="320">
            <template #default="{ row }">
              <a v-if="row.url" :href="row.url" target="_blank" rel="noopener"
                 style="color:#0071e3;text-decoration:none;border-bottom:1px dotted #0071e3"
                 :title="'点击打开商品页面：' + row.title">{{ row.title }}</a>
              <span v-else :title="row.title">{{ row.title }}</span>
              <span class="tag" :class="aiBadge(row).cls" :title="row.img_ok === 1 ? 'AI 图像校验通过' : (row.img_ok === 0 ? 'AI 图像校验未通过' : '未进行 AI 图像校验')">{{ aiBadge(row).txt }}</span>
            </template>
          </el-table-column>
          <el-table-column label="单价" width="110">
            <template #default="{ row }">
              <span :style="{ color: String(row.location || '').indexOf('重庆') >= 0 ? '#0071e3' : '#1d1d1f', fontWeight: 600 }">
                ¥{{ Number(row.price).toFixed(2) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="moq" label="MOQ" width="90">
            <template #default="{ row }"><span style="color:#424245">{{ row.moq || '-' }}</span></template>
          </el-table-column>
          <el-table-column label="供应商" min-width="140">
            <template #default="{ row }">
              <span :title="row.supplier" style="color:#424245;display:inline-block;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom">{{ row.supplier }}</span>
            </template>
          </el-table-column>
          <el-table-column label="发货地" width="110">
            <template #default="{ row }">
              <span v-if="String(row.location || '').indexOf('重庆') >= 0" class="tag loc-hl">{{ row.location }}</span>
              <span v-else style="color:#424245">{{ row.location || '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="纠正" width="110" align="center">
            <template #default="{ row }">
              <el-button v-if="row.img && row.img_ok === 0" type="success" link size="small"
                         @click="reviewImg(row, 1)" title="CLIP 误判了？点击确认这确实该商品，系统会学习该图">
                确实是
              </el-button>
              <el-button v-else-if="row.img && row.img_ok === 1" type="danger" link size="small"
                         @click="reviewImg(row, 0)" title="CLIP 误判了？点击确认这其实不是该商品，系统会学习该图">
                其实不是
              </el-button>
              <span v-else style="color:#ccc;font-size:12px">—</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!allQuotes.length && !loading" style="color:#86868b;padding:20px;text-align:center">暂无数据</div>
        <div style="display:flex;align-items:center;justify-content:flex-end;gap:12px;margin-top:12px">
          <span style="color:#86868b;font-size:12.5px">
            第 {{ page }}/{{ pages }} 页 ｜ 共 {{ allQuotes.length }} 条
          </span>
          <el-button size="small" :disabled="page <= 1" @click="go(-1)">上一页</el-button>
          <el-button size="small" :disabled="page >= pages" @click="go(1)">下一页</el-button>
        </div>
      </div>
    </div>`,
  };

  /* ---------- AI 日报 ---------- */
  const AiReportView = {
    props: { lastReport: Object, aiEnabled: Boolean },
    data() {
      return { content: '', engine: '', ts: '', generating: false, question: '', answer: '', asking: false };
    },
    methods: {
      md2html,
      async generate() {
        this.generating = true;
        try {
          const d = await api('/api/analyze', { method: 'POST' });
          this.content = d.content; this.engine = d.engine; this.ts = d.ts;
          ElMessage.success('日报已生成');
        } catch (e) { ElMessage.error('生成失败: ' + e.message); }
        finally { this.generating = false; }
      },
      async ask() {
        const q = this.question.trim();
        if (!q) return;
        this.asking = true; this.answer = 'AI 思考中…';
        try {
          const d = await api('/api/ask', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question: q }),
          });
          this.answer = d.answer;
        } catch (e) { this.answer = '提问失败: ' + e.message; }
        finally { this.asking = false; }
      },
    },
    mounted() {
      if (this.lastReport) { this.content = this.lastReport.content; this.engine = this.lastReport.engine; this.ts = this.lastReport.ts; }
    },
    template: `
    <div class="content-inner">
      <div class="section-head">
        <div class="kicker">AI Daily Report</div>
        <h2>AI 日报</h2>
        <div class="desc">汇率风险研判 + 政策要点 + 首单建议（智谱 GLM 主通道 → DeepSeek 备用 → 规则引擎兜底）。</div>
      </div>
      <div class="panel">
        <div class="toolbar" style="margin-bottom:10px">
          <el-button type="primary" :loading="generating" @click="generate">
            <span class="iconfont icon-robot" style="margin-right:6px"></span>生成当日日报
          </el-button>
          <span v-if="engine" style="color:#86868b;font-size:12.5px">
            引擎: {{ engine }} ｜ {{ ts }}<span v-if="!aiEnabled">（未配置API Key，规则引擎模式）</span>
          </span>
          <span v-else-if="!aiEnabled" style="color:#86868b;font-size:12.5px">
            未配置 API Key，当前为规则引擎模式（可在 ai_config.json 配置智谱 / DeepSeek Key）
          </span>
        </div>
        <div v-if="content" class="md" style="line-height:1.75" v-html="md2html(content)"></div>
        <div v-else style="color:#86868b;padding:24px 0;text-align:center">尚无日报，点击上方按钮生成</div>
      </div>
      <div class="panel">
        <h3 class="panel-title"><span class="iconfont icon-chat-dots" style="color:#0071e3"></span>政策智能问答</h3>
        <div style="display:flex;gap:10px">
          <el-input v-model="question" placeholder="如：近期有哪些与出口退税相关的政策？" clearable @keyup.enter="ask"></el-input>
          <el-button type="primary" :loading="asking" @click="ask">
            <span class="iconfont icon-search" style="margin-right:6px"></span>提问
          </el-button>
        </div>
        <div v-if="answer" class="md" style="margin-top:14px;padding:14px 16px;background:#fafafc;border-radius:10px;line-height:1.75"
             v-html="md2html(answer)"></div>
      </div>
    </div>`,
  };

  /* ---------- 利润测算 ---------- */
  const CalculatorView = {
    props: { defaults: Object, defaultItem: Object, categories: Array },
    data() {
      const df = this.defaults || {};
      return {
        items: [],
        freightMode: df.freight_mode || 'auto',
        params: {
          freight_base: df.freight_base, freight_per_kg: df.freight_per_kg, freight: df.freight,
          agent_fee: df.agent_fee, misc_fee: df.misc_fee, markup: df.markup,
          deposit_ratio: df.deposit_ratio, ghana_tax: df.ghana_tax, equity_a: df.equity_a,
        },
        result: null,
        goodsList: [],
        importId: '',
        calcTimer: null,
        loading: false,
        // 报价保存
        saveDialogVisible: false,
        quoteName: '',
        quoteNote: '',
        // AI 预测对比
        aiPredicting: false,
        aiPrediction: null,
        // 报价历史
        quotes: [],
        showQuoteList: false,
      };
    },
    computed: {
      freightLabel() { return this.freightMode === 'auto' ? '海运物流费（按重量估算）' : '海运物流费（固定金额）'; },
      catMap() {
        const m = {};
        (this.categories || []).forEach(c => { m[c.key] = c; });
        return m;
      },
      resultCards() {
        const c = this.result; if (!c) return [];
        const eq = Number(this.params.equity_a) || 51;
        return [
          ['商品数 / 总重量', c.count + ' 种 / ' + fmt(c.total_weight) + ' kg'],
          ['采购成本合计', fmtMoney(c.purchase)],
          [this.freightLabel, fmtMoney(c.freight)],
          ['总成本', fmtMoney(c.total_cost)],
          ['对加方报价', fmtMoney(c.quote) + '<div class="sub">' + fmtDollar(c.quote_usd) + '</div>'],
          ['毛利', fmtMoney(c.profit), true],
          ['毛利率', c.margin_pct + '%', true],
          ['定金（先收再采购）', fmtMoney(c.deposit)],
          ['加纳含税到岸成本', fmtDollar(c.ghana_landed_usd)],
          ['两人分成 ' + eq + '/' + (100 - eq), fmtMoney(c.share_a) + ' / ' + fmtMoney(c.share_b)],
        ];
      },
      aiDiffColor() {
        if (!this.aiPrediction) return '';
        return this.aiPrediction.diff_pct > 0 ? '#e53935' : '#43a047';
      },
    },
    methods: {
      fmt, fmtMoney, fmtDouble,
      addRow(item) {
        item = item || {};
        this.items.push({
          name: item.name || '',
          unit_price: item.unit_price ?? '',
          quantity: item.quantity ?? '',
          unit_weight: fmtDouble(item.unit_weight ?? ''),
          category: item.category || '',
        });
        this.scheduleCalc();
      },
      removeRow(idx) {
        if (this.items.length <= 1) { ElMessage.warning('至少保留一种商品'); return; }
        this.items.splice(idx, 1);
        this.scheduleCalc();
      },
      fmtWeight(row) { row.unit_weight = fmtDouble(row.unit_weight); },
      catName(key) { return this.catMap[key]?.name || ''; },
      catTariff(key) {
        const c = this.catMap[key];
        if (!c) return null;
        return Math.round(c.cet_duty + 22.45);
      },
      async loadGoods() {
        try {
          const d = await api('/api/goods');
          this.goodsList = d.goods || [];
        } catch (e) { console.error('goods:', e); }
      },
      importGoods(id) {
        const g = this.goodsList.find(x => String(x.id) === String(id));
        if (!g) return;
        this.addRow({ name: g.name, unit_price: g.unit_price, quantity: g.moq || 1,
                      unit_weight: g.unit_weight, category: g.category });
        this.importId = '';
      },
      scheduleCalc() {
        clearTimeout(this.calcTimer);
        this.calcTimer = setTimeout(() => this.doCalc(), 250);
      },
      async doCalc() {
        const items = this.items.map((row, i) => ({
          name: (row.name || '').trim() || ('商品' + (i + 1)),
          unit_price: parseFloat(row.unit_price) || 0,
          quantity: parseFloat(row.quantity) || 0,
          unit_weight: parseFloat(row.unit_weight) || 0,
          category: row.category || '',
        }));
        if (!items.length) return;
        this.loading = true;
        try {
          this.result = await api('/api/calc', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items, freight_mode: this.freightMode, ...this.params }),
          });
        } catch (e) { ElMessage.error('测算失败: ' + e.message); }
        finally { this.loading = false; }
      },
      // 保存报价单
      openSaveDialog() {
        if (!this.result) { ElMessage.warning('请先完成测算'); return; }
        this.quoteName = '报价单_' + new Date().toLocaleDateString('zh-CN').replace(/\//g, '');
        this.quoteNote = '';
        this.saveDialogVisible = true;
      },
      async doSaveQuote() {
        const items = this.items.map((row, i) => ({
          name: (row.name || '').trim() || ('商品' + (i + 1)),
          unit_price: parseFloat(row.unit_price) || 0,
          quantity: parseFloat(row.quantity) || 0,
          unit_weight: parseFloat(row.unit_weight) || 0,
          category: row.category || '',
        }));
        try {
          await api('/api/quote/save', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              name: this.quoteName, items,
              params: { freight_mode: this.freightMode, ...this.params },
              result: this.result, note: this.quoteNote,
            }),
          });
          ElMessage.success('报价单已保存：' + this.quoteName);
          this.saveDialogVisible = false;
          this.loadQuotes();
        } catch (e) { ElMessage.error('保存失败: ' + e.message); }
      },
      // AI 预测对比
      async doAiPredict() {
        if (!this.result) { ElMessage.warning('请先完成测算'); return; }
        this.aiPredicting = true;
        this.aiPrediction = null;
        try {
          const items = this.items.map((row, i) => ({
            name: (row.name || '').trim() || ('商品' + (i + 1)),
            unit_price: parseFloat(row.unit_price) || 0,
            quantity: parseFloat(row.quantity) || 0,
            unit_weight: parseFloat(row.unit_weight) || 0,
            category: row.category || '',
          }));
          this.aiPrediction = await api('/api/quote/ai-predict', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              items, params: { freight_mode: this.freightMode, ...this.params },
              result: this.result,
            }),
          });
        } catch (e) { ElMessage.error('AI预测失败: ' + e.message); }
        finally { this.aiPredicting = false; }
      },
      // 报价历史
      async loadQuotes() {
        try {
          const d = await api('/api/quotes');
          this.quotes = d.quotes || [];
        } catch (e) { console.error('quotes:', e); }
      },
    },
    mounted() {
      const di = this.defaultItem || { name: '', unit_price: 0, quantity: 0, unit_weight: 0 };
      this.addRow(di);
      this.loadGoods();
      this.loadQuotes();
      this.doCalc();
    },
    beforeUnmount() { clearTimeout(this.calcTimer); },
    watch: {
      freightMode() { this.scheduleCalc(); },
      params: { deep: true, handler() { this.scheduleCalc(); } },
    },
    template: `
    <div class="content-inner">
      <div class="section-head">
        <div class="kicker">Profit Calculator</div>
        <h2>多商品利润测算器</h2>
        <div class="desc">按品类分别计算加纳税负（摩配CET10% vs 成品CET20-35%）；支持保存报价、AI预测对比。</div>
      </div>
      <div class="panel">
        <h3 class="panel-title"><span class="iconfont icon-box-seam" style="color:#0071e3"></span>商品清单
          <span class="tip">（{{ items.length }} 种商品）</span></h3>
        <el-table :data="items" class="goods-table" style="width:100%">
          <el-table-column label="商品名称" min-width="150">
            <template #default="{ row }">
              <el-input v-model="row.name" placeholder="如：摩托车链条" @input="scheduleCalc"></el-input>
            </template>
          </el-table-column>
          <el-table-column label="品类" min-width="140">
            <template #default="{ row }">
              <el-select v-model="row.category" placeholder="选择品类" clearable filterable
                          style="width:100%" @change="scheduleCalc">
                <el-option v-for="c in (categories||[])" :key="c.key" :value="c.key"
                           :label="c.name + ' (CET ' + c.cet_duty + '%)'"></el-option>
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="采购单价（元/件）" min-width="120">
            <template #default="{ row }">
              <el-input-number v-model="row.unit_price" :controls="false" :min="0" :precision="2"
                               style="width:100%" @change="scheduleCalc"></el-input-number>
            </template>
          </el-table-column>
          <el-table-column label="数量（件）" min-width="100">
            <template #default="{ row }">
              <el-input-number v-model="row.quantity" :controls="false" :min="0" :precision="0"
                               style="width:100%" @change="scheduleCalc"></el-input-number>
            </template>
          </el-table-column>
          <el-table-column label="单件重量（kg）" min-width="100">
            <template #default="{ row }">
              <el-input v-model="row.unit_weight" placeholder="如 1.0" @input="scheduleCalc"
                        @blur="fmtWeight(row)"></el-input>
            </template>
          </el-table-column>
          <el-table-column label="小计（元）" min-width="90">
            <template #default="{ row }">
              <span class="g-sub">{{ (parseFloat(row.unit_price) || 0) * (parseFloat(row.quantity) || 0) ? fmtMoney((parseFloat(row.unit_price) || 0) * (parseFloat(row.quantity) || 0)) : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="60" align="center">
            <template #default="{ $index }">
              <el-button type="danger" link @click="removeRow($index)" title="删除该商品">
                <span class="iconfont icon-trash3"></span>
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="toolbar" style="margin-top:12px">
          <el-button @click="addRow()">
            <span class="iconfont icon-plus-lg" style="margin-right:6px"></span>添加商品
          </el-button>
          <el-select v-model="importId" placeholder="从商品库导入…" clearable filterable style="width:300px"
                     @change="importGoods">
            <el-option v-for="g in goodsList" :key="g.id" :value="g.id"
                       :label="g.name + '｜¥' + g.unit_price + '｜' + fmtDouble(g.unit_weight) + 'kg/件'"></el-option>
          </el-select>
        </div>
      </div>
      <div class="panel">
        <h3 class="panel-title"><span class="iconfont icon-truck" style="color:#0071e3"></span>海运物流费</h3>
        <el-radio-group v-model="freightMode" style="margin-bottom:12px">
          <el-radio value="auto">按总重量自动估算</el-radio>
          <el-radio value="fixed">固定金额</el-radio>
        </el-radio-group>
        <div v-if="freightMode === 'auto'" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px">
          <div><div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">基础费（元）</div>
            <el-input-number v-model="params.freight_base" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">每公斤海运费（元/kg）</div>
            <el-input-number v-model="params.freight_per_kg" :controls="false" :min="0" :precision="2" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">预估海运物流费</div>
            <el-input :model-value="result ? fmtMoney(result.freight) : '—'" readonly
                      style="font-weight:600;color:#1d1d1f"></el-input></div>
        </div>
        <div v-else style="max-width:260px">
          <div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">海运物流费（元）</div>
          <el-input-number v-model="params.freight" :controls="false" :min="0" style="width:100%"></el-input-number>
        </div>
      </div>
      <div class="panel">
        <h3 class="panel-title"><span class="iconfont icon-cash-coin" style="color:#0071e3"></span>财务与费用参数</h3>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px">
          <div><div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">报关代理费（元）</div>
            <el-input-number v-model="params.agent_fee" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">寄样验货杂费（元）</div>
            <el-input-number v-model="params.misc_fee" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">加价率（%）</div>
            <el-input-number v-model="params.markup" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">定金比例（%）</div>
            <el-input-number v-model="params.deposit_ratio" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">加纳综合税负（%，未选品类时用）</div>
            <el-input-number v-model="params.ghana_tax" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">重庆合伙人股比（%）</div>
            <el-input-number v-model="params.equity_a" :controls="false" :min="0" :max="100" style="width:100%"></el-input-number></div>
        </div>
      </div>
      <div class="panel" v-loading="loading">
        <h3 class="panel-title"><span class="iconfont icon-currency-exchange" style="color:#0071e3"></span>测算结果</h3>
        <template v-if="result">
          <div class="calc-sub">商品明细（含分品类税负）</div>
          <el-table :data="result.items" size="small" style="width:100%;margin-bottom:8px">
            <el-table-column prop="name" label="商品" min-width="120"></el-table-column>
            <el-table-column label="品类" min-width="100">
              <template #default="{ row }">
                <span v-if="row.category_name">{{ row.category_name }}</span>
                <span v-else style="color:#86868b">未分类</span>
              </template>
            </el-table-column>
            <el-table-column label="数量 × 单价" min-width="130">
              <template #default="{ row }">{{ fmt(row.quantity) }} 件 × ¥{{ row.unit_price }}</template>
            </el-table-column>
            <el-table-column label="采购小计" min-width="100">
              <template #default="{ row }">¥{{ fmt(row.purchase) }}</template>
            </el-table-column>
            <el-table-column label="重量" min-width="80">
              <template #default="{ row }">{{ fmt(row.weight) }} kg</template>
            </el-table-column>
            <el-table-column label="加纳税率" min-width="80">
              <template #default="{ row }">
                <span style="font-weight:600;color:#e53935">{{ row.ghana_tax_pct }}%</span>
              </template>
            </el-table-column>
            <el-table-column label="到岸成本" min-width="90">
              <template #default="{ row }">{{ fmtDollar(row.item_landed_usd) }}</template>
            </el-table-column>
          </el-table>
          <div class="result-cards">
            <div class="card" v-for="c in resultCards" :key="c[0]">
              <div class="label">{{ c[0] }}</div>
              <div class="value" :class="{ green: c[2] }" v-html="c[1]"></div>
            </div>
          </div>
        </template>
        <div v-else style="color:#86868b;padding:18px 0;text-align:center">正在计算…</div>
      </div>
      <div class="panel">
        <h3 class="panel-title">
          <span class="iconfont icon-robot" style="color:#0071e3"></span>报价操作：保存 / AI预测对比
        </h3>
        <div class="toolbar" style="margin-bottom:14px">
          <el-button type="primary" @click="openSaveDialog" :disabled="!result">
            <span class="iconfont icon-floppy-disk" style="margin-right:6px"></span>保存报价单
          </el-button>
          <el-button @click="doAiPredict" :loading="aiPredicting" :disabled="!result">
            <span class="iconfont icon-graph-up" style="margin-right:6px"></span>AI预测对比
          </el-button>
          <el-button text @click="showQuoteList = !showQuoteList">
            {{ showQuoteList ? '收起' : '查看' }}报价历史（{{ quotes.length }}）
          </el-button>
        </div>
        <!-- AI 预测对比面板 -->
        <div v-if="aiPrediction" class="ai-compare-panel">
          <div class="calc-sub" style="margin-bottom:10px">
            <span class="iconfont icon-graph-up" style="color:#0071e3;margin-right:4px"></span>
            AI vs 人工报价对比
            <span class="tag" style="background:#eef2ff;color:#0071e3;margin-left:8px">{{ aiPrediction.engine }}</span>
          </div>
          <div class="compare-grid">
            <div class="compare-col">
              <div class="compare-header" style="color:#1d1d1f">人工报价</div>
              <div class="compare-row"><span>加价率</span><b>{{ aiPrediction.manual_markup }}%</b></div>
              <div class="compare-row"><span>报价</span><b>{{ fmtMoney(aiPrediction.manual_quote) }}</b></div>
              <div class="compare-row"><span>毛利</span><b>{{ fmtMoney(aiPrediction.predicted_profit ? aiPrediction.manual_quote - result.total_cost : 0) }}</b></div>
            </div>
            <div class="compare-col" style="border-left:2px solid #e53935">
              <div class="compare-header" :style="{color: aiDiffColor}">AI建议</div>
              <div class="compare-row"><span>加价率</span><b :style="{color: aiDiffColor}">{{ aiPrediction.predicted_markup }}%</b></div>
              <div class="compare-row"><span>报价</span><b :style="{color: aiDiffColor}">{{ fmtMoney(aiPrediction.predicted_quote) }}</b></div>
              <div class="compare-row"><span>毛利</span><b>{{ fmtMoney(aiPrediction.predicted_profit) }}</b></div>
            </div>
          </div>
          <div class="compare-diff" v-if="aiPrediction.diff_pct !== 0">
            报价差异：<b :style="{color: aiDiffColor}">{{ aiPrediction.diff_pct > 0 ? '+' : '' }}{{ aiPrediction.diff_pct }}%</b>
            <span v-if="aiPrediction.diff_pct > 0">（AI建议更高报价）</span>
            <span v-else>（AI建议更低报价）</span>
          </div>
          <div v-else class="compare-diff" style="color:#86868b">AI与人工报价一致</div>
          <div class="compare-reasoning" v-html="md2html(aiPrediction.reasoning)"></div>
        </div>
        <!-- 报价历史列表 -->
        <div v-if="showQuoteList && quotes.length" style="margin-top:16px">
          <div class="calc-sub">已保存报价单</div>
          <el-table :data="quotes" size="small" style="width:100%">
            <el-table-column prop="name" label="名称" min-width="120"></el-table-column>
            <el-table-column label="报价" min-width="90">
              <template #default="{ row }">{{ fmtMoney(row.quote) }}</template>
            </el-table-column>
            <el-table-column label="毛利" min-width="90">
              <template #default="{ row }">{{ fmtMoney(row.profit) }}</template>
            </el-table-column>
            <el-table-column label="毛利率" min-width="70">
              <template #default="{ row }">{{ row.margin_pct }}%</template>
            </el-table-column>
            <el-table-column prop="created_at" label="时间" min-width="120"></el-table-column>
          </el-table>
        </div>
      </div>
      <!-- 保存报价单对话框 -->
      <el-dialog v-model="saveDialogVisible" title="保存报价单" width="460px">
        <div style="margin-bottom:14px">
          <div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">报价单名称</div>
          <el-input v-model="quoteName" placeholder="如：首单摩配5SKU"></el-input>
        </div>
        <div>
          <div class="label" style="font-size:12px;color:#86868b;margin-bottom:4px">备注</div>
          <el-input v-model="quoteNote" type="textarea" :rows="2" placeholder="可选"></el-input>
        </div>
        <template #footer>
          <el-button @click="saveDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="doSaveQuote">保存</el-button>
        </template>
      </el-dialog>
    </div>`,
  };

  /* ============================================================
     根组件：顶栏 + 侧栏 + 展示栏
     ============================================================ */
  const App = {
    components: { OverviewView, RatesView, PoliciesView, PartsView, AiReportView, CalculatorView },
    data() {
      return {
        activeView: 'overview',
        summary: { latest_rate: null, rates: [], ghs_change_pct: null, policies: [], fetch_log: [],
                   calc_defaults: {}, calc_default_item: {}, categories: [], ai_enabled: false, last_report: null },
        loading: true,
        error: '',
      };
    },
    computed: {
      dateStr() {
        const t = new Date();
        return t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0') + '-' + String(t.getDate()).padStart(2, '0');
      },
      rateText() {
        const r = this.summary.latest_rate;
        return r ? `USD/CNY ${r.usd_cny.toFixed(4)}｜USD/GHS ${r.usd_ghs.toFixed(4)}` : '暂无汇率数据';
      },
      rateTip() { return '最新汇率：USD/CNY 美元兑人民币 ｜ USD/GHS 美元兑加纳塞地'; },
    },
    methods: {
      async loadSummary() {
        this.loading = true;
        try {
          this.summary = await api('/api/summary');
          this.error = '';
        } catch (e) {
          this.error = '数据加载失败: ' + e.message;
          ElMessage.error(this.error);
        } finally { this.loading = false; }
      },
      onSelect(index) { this.activeView = index; },
    },
    mounted() { this.loadSummary(); },
    template: `
    <el-container class="app-shell">
      <el-header class="topbar" height="52px">
        <div class="brand">
          <span class="logo"><span class="iconfont icon-graph-up"></span></span>
          <span>中加贸易智能分析</span>
        </div>
        <div class="top-right">
          <span class="rate-status" :title="rateTip">{{ rateText }}</span>
          <span class="nav-icon" title="用户中心（预留功能位）"><span class="iconfont icon-person-circle"></span></span>
          <span class="nav-icon" title="主题切换（预留功能位）"><span class="iconfont icon-sun"></span></span>
          <span class="nav-icon" title="消息通知（预留功能位）"><span class="iconfont icon-bell"></span></span>
        </div>
      </el-header>
      <el-container class="body">
        <el-aside class="sidebar" width="224px">
          <el-menu :default-active="activeView" class="side-menu" @select="onSelect">
            <el-menu-item index="overview"><span class="iconfont icon-speedometer2 menu-ico"></span>概览</el-menu-item>
            <el-menu-item index="rates"><span class="iconfont icon-graph-up menu-ico"></span>汇率趋势</el-menu-item>
            <el-menu-item index="policies"><span class="iconfont icon-newspaper menu-ico"></span>政策监控</el-menu-item>
            <el-menu-item index="parts"><span class="iconfont icon-tools menu-ico"></span>配件报价</el-menu-item>
            <el-menu-item index="ai"><span class="iconfont icon-robot menu-ico"></span>AI 日报</el-menu-item>
            <el-menu-item index="calculator"><span class="iconfont icon-calculator menu-ico"></span>利润测算</el-menu-item>
          </el-menu>
          <div class="sidebar-foot">
            <div><span class="iconfont icon-database" style="margin-right:4px"></span>数据：汇率 / 政策 / 配件 / 商品库</div>
            <div><span class="iconfont icon-shield-check" style="margin-right:4px"></span>经营红线：先收定金再采购</div>
          </div>
        </el-aside>
        <el-main class="content" v-loading="loading">
          <OverviewView v-if="activeView === 'overview'" :summary="summary" @refresh="loadSummary" />
          <RatesView v-else-if="activeView === 'rates'" :rates="summary.rates" :latest="summary.latest_rate" />
          <PoliciesView v-else-if="activeView === 'policies'" :policies="summary.policies" />
          <PartsView v-else-if="activeView === 'parts'" />
          <AiReportView v-else-if="activeView === 'ai'" :last-report="summary.last_report" :ai-enabled="summary.ai_enabled" />
          <CalculatorView v-else :defaults="summary.calc_defaults" :default-item="summary.calc_default_item" :categories="summary.categories" />
        </el-main>
      </el-container>
    </el-container>`,
  };

  /* ---------------- 启动 ---------------- */
  const app = createApp(App);
  for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
    app.component(name, comp);
  }
  app.use(ElementPlus);
  app.mount('#app');
})();
