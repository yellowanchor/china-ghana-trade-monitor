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
      reportHtml() {
        const lr = this.summary.last_report;
        if (!lr || !lr.content) return '';
        return md2html(lr.content);
      },
    },
    methods: {
      md2html,
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
        <div class="panel ov-ai-panel">
          <h3 class="panel-title"><span class="iconfont icon-magic" style="color:#0071e3"></span>AI 分析日报
            <span class="tip">{{ summary.last_report ? summary.last_report.ts : '暂无' }}</span>
            <span v-if="summary.last_report && summary.last_report.engine" class="engine-badge" :title="'AI 引擎：' + summary.last_report.engine">{{ summary.last_report.engine }}</span>
            <span v-else-if="!summary.ai_enabled" class="engine-badge rule" title="未配置 API Key，当前为规则引擎模式">规则引擎</span>
          </h3>
          <div v-if="reportHtml" class="md ov-report" style="color:#424245" v-html="reportHtml"></div>
          <div v-else style="color:#86868b;padding:12px 0">
            {{ summary.ai_enabled ? '尚未生成日报，可进入「AI 日报」生成。' : '未配置 API Key，当前为规则引擎模式。' }}
          </div>
        </div>
      </div>
      <div class="panel kb-panel">
        <h3 class="panel-title"><span class="iconfont icon-book" style="color:#0071e3"></span>基础知识文档
          <span class="tip">外贸术语 / 认证 / 经营规则</span></h3>
        <div class="kb-grid">
          <div class="kb-section">
            <h4><span class="iconfont icon-truck" style="margin-right:4px"></span>单位与术语</h4>
            <dl>
              <dt>海运 / 票</dt>
              <dd>一票 = 一个提单（B/L）对应的一批货物。不同票需分别报关、分别计费。海运费按票收取基础费 + 重量/体积附加。</dd>
              <dt>TEU / 柜</dt>
              <dd>TEU = Twenty-foot Equivalent Unit（20英尺标准集装箱）。1个20尺柜=1 TEU，1个40尺柜=2 TEU。整柜（FCL）走海运，散货走拼箱（LCL）。20尺柜内容积约33 CBM，限重约28吨；40尺柜约67 CBM，限重约30吨。</dd>
              <dt>LCL / FCL</dt>
              <dd>LCL（Less than Container Load）= 拼箱，多个发货人共用一个柜，按CBM或吨计费（取大者），适合首单小批量（<15 CBM）。FCL（Full Container Load）= 整柜，一个柜一个发货人，按柜计费，单价更低。首单约1万美元货值建议LCL，量大后转FCL。</dd>
              <dt>CBM</dt>
              <dd>Cubic Meter（立方米），海运散货体积计量单位。1 CBM海运限重约1吨（重货）或363 kg（泡货）。海运费按"CBM或吨，取大者"计收（即重货按吨、泡货按CBM）。计算公式：长(m)×宽(m)×高(m)。</dd>
              <dt>MOQ</dt>
              <dd>Minimum Order Quantity（最低起订量），供应商要求的最小采购数量。低于MOQ无法下单或需加价。批量越大单价越低，但首单不宜过量囤货。</dd>
              <dt>HS Code</dt>
              <dd>Harmonized System Code（海关商品编码），国际通用6位+各国扩展位。中国10位，加纳8位。决定了进口关税税率。摩配常用HS：8714100（摩托车零件）、8708990（汽车零件）。</dd>
              <dt>EXW / FOB / CIF</dt>
              <dd>贸易术语：① EXW（Ex Works）工厂交货，买方承担从工厂到目的地的全部费用和风险（卖方只负责在工厂交货）。② FOB（Free On Board）船上交货，卖方负责将货物交到装运港船上，离港后买方承担运费保险。③ CIF（Cost Insurance Freight）成本+保险+运费，卖方负责到目的港的运费和保险，但风险在装船后转移给买方。本项目首单建议FOB或CIF。</dd>
              <dt>商业发票（Commercial Invoice）</dt>
              <dd>出口报关核心单据，包含买卖双方信息、商品描述、HS Code、单价、总价、币种、贸易术语（如FOB Chongqing）、付款方式等。加纳清关需用商业发票计算CIF值以确定税基。</dd>
              <dt>装箱单（Packing List）</dt>
              <dd>列明每件/每箱的品名、数量、毛重（Gross Weight）、净重（Net Weight）、体积（CBM）、箱号唛头的清单。与商业发票配套使用，海关和物流凭此验货。</dd>
              <dt>滞港费（Demurrage）</dt>
              <dd>集装箱在目的港超过免费堆存期（通常7天）后，码头按天收取的超期费用。加纳特马港（Tema Port）约50-150美元/天/柜。必须提前指定清关代理，货物到港后尽快清关提货，避免滞港费累积。</dd>
            </dl>
          </div>
          <div class="kb-section">
            <h4><span class="iconfont icon-shield-check" style="margin-right:4px"></span>认证与合规</h4>
            <dl>
              <dt>COC</dt>
              <dd>Certificate of Conformity（符合性证书），加纳进口强制认证，由加纳标准局（GSA）授权第三方机构出具。证明产品符合加纳标准。无COC货物将被海关扣留或强制退运。办理流程：装船前在出口国申请验货→验货通过出具COC→随货到港清关。费用约货值的0.5%-1.5%。</dd>
              <dt>SONCAP</dt>
              <dd>Standards Organization of Nigeria Conformity Assessment Programme，尼日利亚符合性评定，流程类似COC但针对尼日利亚市场。若未来拓展尼日利亚市场需办理。</dd>
              <dt>CE / FCC</dt>
              <dd>CE = 欧盟产品安全认证标志（Conformité Européenne），电子产品出口欧盟/非洲多数国家认可；FCC = 美国联邦通信委员会认证（电子产品电磁兼容）。出口目的地不同要求不同。加纳认可CE标志的产品。</dd>
              <dt>FORM E / 原产地证</dt>
              <dd>原产地证书（Certificate of Origin），证明货物原产国。中国-部分非洲国家有优惠关税安排时，凭原产地证可享受减免关税。普通原产地证(CO)在贸促会或海关办理；FORM E用于中国-东盟自贸区。加纳目前无双边自贸协定，但ECOWAS（西共体）成员国有区域关税优惠。</dd>
              <dt>B/L</dt>
              <dd>Bill of Lading（提单），承运人签发的货物收据 + 物权凭证 + 运输合同证明。正本提单（Original B/L）3份，收货人凭正本提单提货。电放提单（Telex Release / Surrendered B/L）无需寄正本，发货人电告承运人放货——本项目尾款付清后电放提单是经营红线之一。</dd>
              <dt>ETC</dt>
              <dd>出口退税登记证，中国出口企业办理退税时需提供的资质证明。出口企业凭报关单、增值税发票等向税务局申请退税，退还国内环节已缴纳的增值税。摩配退税率约13%，可显著降低成本。</dd>
              <dt>商检（CIQ）</dt>
              <dd>中国进出口商品检验检疫。部分商品法定检验（法检），出口前需经海关检验合格出具《出境货物通关单》。非法检商品可免检出口。</dd>
            </dl>
          </div>
          <div class="kb-section">
            <h4><span class="iconfont icon-cash-coin" style="margin-right:4px"></span>条款与经营规则</h4>
            <dl>
              <dt>先收定金再采购 <span style="color:#e53935;font-size:11px">经营红线</span></dt>
              <dd><b>经营红线①</b>：收到客户定金后才能向供应商下单，降低库存与资金风险。定金未到账不得排产发货。这是本项目的零垫资模式核心——用客户的钱做客户的生意。</dd>
              <dt>尾款付清才电放提单 <span style="color:#e53935;font-size:11px">经营红线</span></dt>
              <dd><b>经营红线②</b>：提单 = 货权。货物到港后，必须收到100%尾款才能电放提单让收货人提货。提前电放=丧失货权=可能钱货两空。</dd>
              <dt>对公账户收汇 <span style="color:#e53935;font-size:11px">经营红线</span></dt>
              <dd><b>经营红线③</b>：货款只走外贸代理对公账户或1039市场采购平台合规结汇，严禁个人账户收经营性外汇。违规可能涉嫌逃汇，面临行政处罚。</dd>
              <dt>严禁买单出口 <span style="color:#e53935;font-size:11px">经营红线</span></dt>
              <dd><b>经营红线④</b>：出口走外贸代理或1039市场采购贸易方式（重庆大足试点），严禁买单出口（借他人抬头报关）。买单出口无退税、单据不合规、风险极大。</dd>
              <dt>定金比例</dt>
              <dd>默认30%（覆盖采购成本+海运基础费），大额订单可协商30%-50%。尾款在发货前（FOB/CIF条件下船开前）结清。建议合同约定：T/T 30%预付定金 + T/T 70%发货前付清尾款。</dd>
              <dt>付款方式 T/T vs L/C</dt>
              <dd>T/T（电汇，Telegraphic Transfer）为主：速度快、费用低、操作简单，但依赖信任。L/C（信用证，Letter of Credit）为大额备选（>5万美元）：银行信用担保，安全性高，但手续费高（约0.1%-0.25%）、流程复杂、需满足单据100%相符原则。建议首单用T/T，金额超5万美元后可转L/C。</dd>
              <dt>CIF 价格构成</dt>
              <dd>CIF = 成本（采购价）+ 保险（CIF×0.4%≈货值千四）+ 海运费 + 报关代理费 + 杂费。实际操作中还需加上加价率（默认21%）形成最终报价。注意：CIF条件下卖方负责到港运费保险，但风险在装船后转移给买方。</dd>
              <dt>加纳综合税负</dt>
              <dd>含进口关税 + CET（共同对外关税）+ VAT（增值税14.5%）+ NHIL（国家健康保险税2.5%）+ GETFL（教育信托基金税2.5%）+ 各种手续费。摩配（HS 8714）：CET约10%→综合约32.45%；成品摩托车：CET约20-35%→综合约57.45%。税负是定价的关键变量。</dd>
              <dt>股比分配</dt>
              <dd>重庆方 51% / 加纳方 49%（合伙人协议约定），利润按此比例分成。51%保证重庆方对公司的实际控制权（>50%即为控股权）。</dd>
              <dt>报价公式</dt>
              <dd>总成本 ×（1 + 加价率）= 报价。总成本 = 采购成本 + 海运费 + 报关代理费 + 杂费。默认加价率21%，可根据品类竞争情况浮动（摩配可至25-30%，日用品15-20%）。报价以美元计价（经营红线①）。</dd>
              <dt>出口退税</dt>
              <dd>中国出口企业可申请退还货物在国内生产流通环节已缴纳的增值税。摩配退税率约13%。退税款是重要的利润来源——实际采购成本 = 含税采购价 - 退税款。但需有合规的单据链（增值税专用发票 + 报关单 + 出口发票）。</dd>
              <dt>合同审核要点</dt>
              <dd>重点核查：① 币种是否为USD（不接受人民币/塞地结算）；② 定金比例≥30%；③ 尾款条款是否为"发货前付清"或"见提单复印件付清"（不接受到港后付款）；④ 仲裁/争议解决条款；⑤ 品名数量与报价单一致；⑥ 交货条款（FOB/CIF）与实际一致；⑦ COC费用归属。</dd>
            </dl>
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

  /* ---------- 合同审核 ---------- */
  const ContractView = {
    data() {
      return {
        title: '',
        content: '',
        reviewing: false,
        result: null,          // {engine, review, risk_level, risk_score, ts, id}
        showHistory: false,
        history: [],
        loadingHistory: false,
      };
    },
    computed: {
      riskColor() {
        if (!this.result) return '';
        return { low: '#34c759', medium: '#ff9500', high: '#e53935' }[this.result.risk_level] || '';
      },
      riskLabel() {
        if (!this.result) return '';
        return { low: '低风险', medium: '中风险', high: '高风险' }[this.result.risk_level] || '';
      },
      canReview() { return this.content.trim().length > 10; },
    },
    methods: {
      md2html,
      async loadTemplate() {
        try {
          const d = await api('/api/contract/template');
          this.content = d.template;
          this.title = '销售合同_' + new Date().toLocaleDateString('zh-CN').replace(/\//g, '');
          ElMessage.success('已加载标准合同模板，请编辑后审核');
        } catch (e) { ElMessage.error('加载模板失败: ' + e.message); }
      },
      async doReview() {
        if (!this.canReview) { ElMessage.warning('合同内容太短，请输入完整合同文本'); return; }
        this.reviewing = true;
        this.result = null;
        try {
          this.result = await api('/api/contract/review', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: this.title, content: this.content }),
          });
          ElMessage.success('AI 合同审核完成');
        } catch (e) { ElMessage.error('审核失败: ' + e.message); }
        finally { this.reviewing = false; }
      },
      async loadHistory() {
        this.loadingHistory = true;
        try {
          const d = await api('/api/contract/history');
          this.history = d.contracts || [];
        } catch (e) { console.error('contract history:', e); }
        finally { this.loadingHistory = false; }
      },
      toggleHistory() {
        this.showHistory = !this.showHistory;
        if (this.showHistory && !this.history.length) this.loadHistory();
      },
      async loadContract(id) {
        try {
          const d = await api('/api/contract/' + id);
          this.title = d.title || '';
          this.content = d.content || '';
          this.result = { engine: d.engine, review: d.review_result, risk_level: d.risk_level,
                          risk_score: d.risk_score, ts: (d.created_at || '').slice(0, 16), id: d.id };
          ElMessage.success('已加载历史合同审核记录');
        } catch (e) { ElMessage.error('加载失败: ' + e.message); }
      },
      fmtDate(s) { return String(s || '').slice(0, 16); },
    },
    mounted() {
      // 默认加载模板让用户看到
      this.loadTemplate();
    },
    template: `
    <div class="content-inner">
      <div class="section-head">
        <div class="kicker">Contract Review</div>
        <h2>合同审核</h2>
        <div class="desc">AI 审核贸易合同，逐条检查经营红线、付款条款、合规认证等（默认优先使用 DeepSeek 引擎，多通道容灾）。</div>
      </div>
      <div class="contract-layout">
        <div class="contract-editor panel">
          <h3 class="panel-title"><span class="iconfont icon-file-earmark-text" style="color:#0071e3"></span>合同文本
            <span class="tip">编辑或粘贴合同全文</span></h3>
          <div class="toolbar" style="margin-bottom:10px">
            <el-input v-model="title" placeholder="合同标题" style="width:280px"></el-input>
            <el-button @click="loadTemplate">
              <span class="iconfont icon-file-earmark-plus" style="margin-right:6px"></span>加载模板
            </el-button>
            <el-button type="primary" :loading="reviewing" :disabled="!canReview" @click="doReview">
              <span class="iconfont icon-magic" style="margin-right:6px"></span>AI 审核
            </el-button>
            <el-button text @click="toggleHistory">
              {{ showHistory ? '收起' : '查看' }}审核历史（{{ history.length }}）
            </el-button>
          </div>
          <el-input v-model="content" type="textarea" :rows="22"
            placeholder="在此粘贴或编辑合同全文…可点击"加载模板"使用标准合同模板"
            style="font-family:monospace;font-size:12.5px"></el-input>
        </div>
        <div class="contract-result panel">
          <h3 class="panel-title">
            <span class="iconfont icon-shield-check" style="color:#0071e3"></span>审核结果
            <span v-if="result" class="engine-badge" :title="'审核引擎：' + result.engine">{{ result.engine }}</span>
          </h3>
          <div v-if="reviewing" style="color:#86868b;padding:30px 0;text-align:center">
            <span class="iconfont icon-arrow-clockwise" style="font-size:24px;animation:spin 1s linear infinite;display:inline-block"></span>
            <p style="margin-top:10px">AI 正在审核合同，约 10-30 秒…</p>
          </div>
          <div v-else-if="result" style="overflow:hidden">
            <div class="risk-banner" :style="{borderColor: riskColor}">
              <span class="risk-label" :style="{color: riskColor}">{{ riskLabel }}</span>
              <span style="color:#86868b;font-size:12.5px">风险扣分 {{ 100 - (result.risk_score || 0) }}/100</span>
              <span style="color:#86868b;font-size:12.5px"> ｜ {{ result.ts }}</span>
            </div>
            <div class="md contract-review-md" v-html="md2html(result.review)"></div>
          </div>
          <div v-else style="color:#86868b;padding:30px 0;text-align:center">
            点击「AI 审核」按钮，开始审核合同文本
          </div>
        </div>
      </div>
      <div v-if="showHistory && history.length" class="panel" v-loading="loadingHistory">
        <h3 class="panel-title"><span class="iconfont icon-clock" style="color:#0071e3"></span>审核历史</h3>
        <el-table :data="history" size="small" style="width:100%">
          <el-table-column prop="title" label="合同标题" min-width="180"></el-table-column>
          <el-table-column label="风险等级" width="100">
            <template #default="{ row }">
              <span :style="{color: {low:'#34c759',medium:'#ff9500',high:'#e53935'}[row.risk_level], fontWeight:600}">
                {{ {low:'低',medium:'中',high:'高'}[row.risk_level] }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="engine" label="引擎" width="140"></el-table-column>
          <el-table-column label="时间" width="140">
            <template #default="{ row }"><span style="color:#86868b">{{ fmtDate(row.created_at) }}</span></template>
          </el-table-column>
          <el-table-column label="操作" width="80" align="center">
            <template #default="{ row }">
              <el-button type="primary" link size="small" @click="loadContract(row.id)">查看</el-button>
            </template>
          </el-table-column>
        </el-table>
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
    components: { OverviewView, RatesView, PoliciesView, PartsView, AiReportView, ContractView, CalculatorView },
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
          <span class="logo"><span class="iconfont icon-globe2"></span></span>
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
            <el-menu-item index="overview"><span class="iconfont icon-grid-3x3-gap menu-ico"></span>概览</el-menu-item>
            <el-menu-item index="rates"><span class="iconfont icon-graph-up-arrow menu-ico"></span>汇率趋势</el-menu-item>
            <el-menu-item index="policies"><span class="iconfont icon-journal-text menu-ico"></span>政策监控</el-menu-item>
            <el-menu-item index="parts"><span class="iconfont icon-box-seam menu-ico"></span>配件报价</el-menu-item>
            <el-menu-item index="ai"><span class="iconfont icon-magic menu-ico"></span>AI 日报</el-menu-item>
            <el-menu-item index="contract"><span class="iconfont icon-file-earmark-text menu-ico"></span>合同审核</el-menu-item>
            <el-menu-item index="calculator"><span class="iconfont icon-cash-coin menu-ico"></span>利润测算</el-menu-item>
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
          <ContractView v-else-if="activeView === 'contract'" />
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
