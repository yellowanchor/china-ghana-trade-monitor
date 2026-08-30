/* ============================================================
   中加贸易智能分析 —— 前端应用（Vue3 + Element Plus + ECharts + Iconfont）
   纯浏览器端全局构建版：无打包工具，随 trade_monitor.py 静态托管
   ============================================================ */
(function () {
  'use strict';

  const { createApp, ref, reactive, computed, onMounted, onBeforeUnmount, watch, nextTick } = Vue;
  const ElMessage = ElementPlus.ElMessage;
  const ElMessageBox = ElementPlus.ElMessageBox;

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
  // 简易 Markdown 渲染（标题/列表/引用/加粗/表格）
  // 新增 GFM 风格表格支持：| 表头 | ... | + |---| 分隔行 → <table>，
  // 解决大模型输出的价格表（Markdown 表格）此前被渲染为纯文本的问题。
  function md2html(s) {
    const esc = String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
    const lines = esc.split('\n');
    const out = [];
    let inUl = false;
    // 单元格切分：去掉首尾竖线，按 | 拆分并 trim，单元格内同时处理加粗
    const splitRow = line => {
      let t = line.trim();
      if (t.startsWith('|')) t = t.slice(1);
      if (t.endsWith('|')) t = t.slice(0, -1);
      return t.split('|').map(c => c.trim().replace(/\*\*(.+?)\*\*/g, '<b>$1</b>'));
    };
    const isTableRow = line => /^\s*\|.*\|\s*$/.test(line);
    const isSepRow = line => /^\s*\|[\s:|-]+\|\s*$/.test(line);
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const l = line.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
      // —— 表格块：当前行是表格行，且下一行是 |---| 分隔行 ——
      if (isTableRow(line) && i + 1 < lines.length && isSepRow(lines[i + 1])) {
        if (inUl) { out.push('</ul>'); inUl = false; }
        const headers = splitRow(line);
        out.push('<table><thead><tr>');
        for (const h of headers) out.push('<th>' + h + '</th>');
        out.push('</tr></thead><tbody>');
        i += 2;                                   // 跳过表头 + 分隔行
        while (i < lines.length && isTableRow(lines[i])) {
          const cells = splitRow(lines[i]);
          out.push('<tr>');
          for (const c of cells) out.push('<td>' + c + '</td>');
          out.push('</tr>');
          i++;
        }
        out.push('</tbody></table>');
        i--;                                      // 回退，让外层循环处理下一行
      } else if (/^#{1,3} /.test(l)) {
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
    const ct = res.headers.get('content-type') || '';
    const isJson = ct.indexOf('json') >= 0;
    const data = isJson ? await res.json().catch(() => null) : null;
    if (!res.ok) {
      // 透传后端 JSON error 文案（如「当前密码错误，身份验证失败」），便于前端展示
      const msg = (data && data.error) ? data.error : ('HTTP ' + res.status);
      throw new Error(msg);
    }
    return data !== null ? data : res;
  }

  /* ---------------- AI 模型选择（全局共享） ----------------
     各 AI 部分（日报/问答/二手车/合同/报价预测）共用同一份模型列表，
     首次登录后加载一次；下拉默认「自动选择」（后端自动容灾），可选具体模型。
  */
  const aiModelStore = reactive({ models: [], loaded: false });
  async function loadAiModels() {
    try {
      const d = await api('/api/ai/models');
      aiModelStore.models = d.models || [];
      aiModelStore.loaded = true;
    } catch (e) { aiModelStore.loaded = true; /* 加载失败不阻塞 */ }
  }
  // 可复用模型选择器（v-model 绑定模型 id，空串 = 自动）
  const ModelSelect = {
    props: { modelValue: { type: String, default: '' } },
    emits: ['update:modelValue'],
    computed: {
      models() { return aiModelStore.models; },
    },
    methods: {
      onInput(v) { this.$emit('update:modelValue', v || ''); },
    },
    template: `
      <el-select :model-value="modelValue" size="small" class="model-select"
                 @update:model-value="onInput" :placeholder="'自动'">
        <el-option label="自动选择" value="" />
        <el-option v-for="m in models" :key="m.id" :value="m.id"
          :label="(m.name || m.id) + (m.free ? ' · 免费' : '')"
          :disabled="!m.available">
          <span>{{ m.name || m.id }}</span>
          <span v-if="m.free" class="model-free-tag">免费</span>
          <span v-else-if="!m.available" class="model-off-tag">未配置</span>
          <span v-if="m.quota" class="model-quota">{{ m.quota }}</span>
        </el-option>
      </el-select>`,
  };

  /* ---------------- 主题管理 ----------------
     模式三态：auto（跟随系统，默认）/ light（白天）/ dark（夜晚）
     - 持久化到 localStorage('tm-theme')
     - 生效方式：html 根节点加/去 .dark 类（CSS 变量 + Element Plus 暗色变量同时切换）
     - 切换时广播 window 'themechange' 事件，ECharts 等组件监听后重绘
  */
  const themeStore = reactive({
    mode: (function () {
      const m = localStorage.getItem('tm-theme');
      return m === 'light' || m === 'dark' ? m : 'auto';
    })(),
  });

  function systemPrefersDark() {
    return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  }
  function effectiveTheme() {
    return themeStore.mode === 'auto' ? (systemPrefersDark() ? 'dark' : 'light') : themeStore.mode;
  }
  function applyTheme() {
    const dark = effectiveTheme() === 'dark';
    document.documentElement.classList.toggle('dark', dark);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', dark ? '#161618' : '#f5f5f7');
    window.dispatchEvent(new CustomEvent('themechange', { detail: { dark } }));
  }
  function setThemeMode(mode) {
    themeStore.mode = mode;
    localStorage.setItem('tm-theme', mode);
    applyTheme();
  }
  // 点击循环：跟随系统 → 白天 → 夜晚 → 跟随系统
  function cycleTheme() {
    setThemeMode(themeStore.mode === 'auto' ? 'light' : themeStore.mode === 'light' ? 'dark' : 'auto');
  }
  // 系统主题变化时，auto 模式下实时跟随（无需刷新）
  (function watchSystemTheme() {
    if (!window.matchMedia) return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const onSys = () => { if (themeStore.mode === 'auto') applyTheme(); };
    if (mq.addEventListener) mq.addEventListener('change', onSys);
    else if (mq.addListener) mq.addListener(onSys);
  })();
  // 初始化（index.html 里的预置脚本已提前设好 class，这里补广播 + 校正）
  applyTheme();

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
        <span v-if="msg" style="color:var(--text-3);font-size:12.5px">{{ msg }}</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px" class="ov-grid">
        <div class="panel">
          <h3 class="panel-title"><span class="iconfont icon-newspaper" style="color:var(--accent)"></span>政策快览
            <span class="tip">最新 {{ topPolicies.length }} 条</span></h3>
          <div v-if="topPolicies.length">
            <div v-for="p in topPolicies" :key="p.id" style="padding:7px 0;border-bottom:0.5px solid #f0f0f2">
              <div style="display:flex;justify-content:space-between;gap:10px">
                <a :href="p.url" target="_blank" rel="noopener"
                   style="color:var(--text);text-decoration:none;line-height:1.5" :title="p.title">{{ p.title }}</a>
              </div>
              <div style="font-size:12px;color:var(--text-3);margin-top:2px">{{ p.source }}
                <span v-if="p.keyword" class="tag">{{ p.keyword.split(',')[0] }}</span>
                <span v-if="p.is_new" class="tag new">今日新增</span>
              </div>
            </div>
          </div>
          <div v-else style="color:var(--text-3);padding:12px 0">暂无政策数据，请点击「立即采集一次」</div>
        </div>
        <div class="panel ov-ai-panel">
          <h3 class="panel-title"><span class="iconfont icon-magic" style="color:var(--accent)"></span>AI 分析日报
            <span class="tip">{{ summary.last_report ? summary.last_report.ts : '暂无' }}</span>
            <span v-if="summary.last_report && summary.last_report.engine" class="engine-badge" :title="'AI 引擎：' + summary.last_report.engine">{{ summary.last_report.engine }}</span>
            <span v-else-if="!summary.ai_enabled" class="engine-badge rule" title="未配置 API Key，当前为规则引擎模式">规则引擎</span>
          </h3>
          <div v-if="reportHtml" class="md ov-report" style="color:var(--text-2)" v-html="reportHtml"></div>
          <div v-else style="color:var(--text-3);padding:12px 0">
            {{ summary.ai_enabled ? '尚未生成日报，可进入「AI 日报」生成。' : '未配置 API Key，当前为规则引擎模式。' }}
          </div>
        </div>
      </div>
      <div class="panel kb-panel">
        <h3 class="panel-title"><span class="iconfont icon-book" style="color:var(--accent)"></span>基础知识文档
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
              <dt>先收定金再采购 <span style="color:var(--red-strong);font-size:11px">经营红线</span></dt>
              <dd><b>经营红线①</b>：收到客户定金后才能向供应商下单，降低库存与资金风险。定金未到账不得排产发货。这是本项目的零垫资模式核心——用客户的钱做客户的生意。</dd>
              <dt>尾款付清才电放提单 <span style="color:var(--red-strong);font-size:11px">经营红线</span></dt>
              <dd><b>经营红线②</b>：提单 = 货权。货物到港后，必须收到100%尾款才能电放提单让收货人提货。提前电放=丧失货权=可能钱货两空。</dd>
              <dt>对公账户收汇 <span style="color:var(--red-strong);font-size:11px">经营红线</span></dt>
              <dd><b>经营红线③</b>：货款只走外贸代理对公账户或1039市场采购平台合规结汇，严禁个人账户收经营性外汇。违规可能涉嫌逃汇，面临行政处罚。</dd>
              <dt>严禁买单出口 <span style="color:var(--red-strong);font-size:11px">经营红线</span></dt>
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
        // 图表配色随主题切换（暗色下轴线/文字用亮灰，避免黑底黑字）
        const dark = document.documentElement.classList.contains('dark');
        const axisFg = dark ? '#8e8e95' : '#86868b';
        const axisLine = dark ? '#4a4a4f' : '#d2d2d7';
        const splitLine = dark ? '#38383c' : '#e8e8ed';
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
          legend: { data: ['USD/CNY', 'USD/GHS'], textStyle: { color: axisFg }, top: 4 },
          grid: { left: 52, right: 52, top: 44, bottom: 30 },
          xAxis: { type: 'category', data: this.dates, axisLine: { lineStyle: { color: axisLine } },
                   axisLabel: { color: axisFg } },
          yAxis: [
            { type: 'value', name: '兑CNY', nameTextStyle: { color: axisFg },
              axisLabel: { color: axisFg }, splitLine: { lineStyle: { color: splitLine } } },
            { type: 'value', name: '兑GHS', nameTextStyle: { color: axisFg },
              axisLabel: { color: axisFg }, splitLine: { show: false } },
          ],
          series: [
            { name: 'USD/CNY', type: 'line', smooth: true, data: (this.rates || []).map(x => x.usd_cny),
              symbolSize: 6, itemStyle: { color: '#ff3b30' }, lineStyle: { width: 2.5 } },
            { name: 'USD/GHS', type: 'line', smooth: true, yAxisIndex: 1,
              symbolSize: 6, data: (this.rates || []).map(x => x.usd_ghs),
              itemStyle: { color: dark ? '#409cff' : '#0071e3' }, lineStyle: { width: 2.5 } },
          ],
        });
      },
      onResize() { if (this.chart) this.chart.resize(); },
      onTheme() { this.render(); },
    },
    mounted() {
      nextTick(() => {
        this.chart = echarts.init(this.$refs.chartBox);
        this.render();
      });
      window.addEventListener('resize', this.onResize);
      window.addEventListener('themechange', this.onTheme);
    },
    beforeUnmount() {
      window.removeEventListener('resize', this.onResize);
      window.removeEventListener('themechange', this.onTheme);
      if (this.chart) { this.chart.dispose(); this.chart = null; }
    },
    activated() {
      // keep-alive 切回本视图时 DOM 重新插入，图表需 resize 适应容器尺寸
      if (this.chart) this.$nextTick(() => this.chart.resize());
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
        <div v-if="!hasData" style="color:var(--text-3);text-align:center;padding:30px">暂无汇率数据，请先采集</div>
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
        <span style="color:var(--text-3);font-size:12.5px">
          共 {{ filtered.length }} 条<span v-if="todayCount"> ｜ 今日新增 {{ todayCount }} 条</span>
        </span>
      </div>
      <div class="panel" style="padding:8px 12px">
        <el-table :data="filtered" size="default" style="width:100%">
          <el-table-column prop="source" label="来源" width="130">
            <template #default="{ row }">
              <span style="font-weight:600;color:var(--text-2)">{{ row.source }}</span>
            </template>
          </el-table-column>
          <el-table-column label="标题" min-width="420">
            <template #default="{ row }">
              <a :href="row.url" target="_blank" rel="noopener" :title="row.title"
                 style="color:var(--text);text-decoration:none">{{ row.title }}</a>
              <span v-if="row.keyword" class="tag">{{ row.keyword.split(',')[0] }}</span>
              <span v-if="row.is_new" class="tag new">今日新增</span>
            </template>
          </el-table-column>
          <el-table-column label="收录时间" width="120">
            <template #default="{ row }">
              <span style="color:var(--text-3);font-size:12.5px">{{ String(row.first_seen).slice(0, 10) }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!filtered.length" style="color:var(--text-3);padding:20px;text-align:center">
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
        <span v-if="fetchMsg" style="color:var(--text-3);font-size:12.5px">{{ fetchMsg }}</span>
      </div>
      <div class="grid-cards">
        <div class="card" style="cursor:pointer" v-for="s in stats" :key="s.keyword" @click="filterKw(s.keyword)">
          <div class="label">{{ s.keyword }}</div>
          <div class="value" style="font-size:18px">¥{{ Number(s.min_price).toFixed(2) }}<span style="font-size:12px;color:var(--text-3)"> 起</span></div>
          <div class="note">均价 ¥{{ s.avg_price }} ｜ {{ s.n }} 条</div>
        </div>
        <div v-if="!stats.length" class="card" style="grid-column:1/-1;color:var(--text-3)">暂无报价数据，点击上方按钮抓取</div>
      </div>
      <div class="toolbar" v-if="stats.length">
        <span style="font-size:12px;color:var(--text-3)">筛选：</span>
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
              <span v-else style="color:var(--placeholder-fg)">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="keyword" label="关键词" width="120">
            <template #default="{ row }"><span style="color:var(--text-2)">{{ row.keyword }}</span></template>
          </el-table-column>
          <el-table-column label="标题" min-width="320">
            <template #default="{ row }">
              <a v-if="row.url" :href="row.url" target="_blank" rel="noopener"
                 style="color:var(--accent);text-decoration:none;border-bottom:1px dotted var(--accent)"
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
            <template #default="{ row }"><span style="color:var(--text-2)">{{ row.moq || '-' }}</span></template>
          </el-table-column>
          <el-table-column label="供应商" min-width="140">
            <template #default="{ row }">
              <span :title="row.supplier" style="color:var(--text-2);display:inline-block;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom">{{ row.supplier }}</span>
            </template>
          </el-table-column>
          <el-table-column label="发货地" width="110">
            <template #default="{ row }">
              <span v-if="String(row.location || '').indexOf('重庆') >= 0" class="tag loc-hl">{{ row.location }}</span>
              <span v-else style="color:var(--text-2)">{{ row.location || '-' }}</span>
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
              <span v-else style="color:var(--placeholder-fg);font-size:12px">—</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!allQuotes.length && !loading" style="color:var(--text-3);padding:20px;text-align:center">暂无数据</div>
        <div style="display:flex;align-items:center;justify-content:flex-end;gap:12px;margin-top:12px">
          <span style="color:var(--text-3);font-size:12.5px">
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
      return { content: '', engine: '', ts: '', generating: false, question: '', answer: '', asking: false, model: '' };
    },
    methods: {
      md2html,
      async generate() {
        this.generating = true;
        try {
          const d = await api('/api/analyze', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model: this.model }),
          });
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
            body: JSON.stringify({ question: q, model: this.model }),
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
          <ModelSelect v-model="model" />
          <el-button type="primary" :loading="generating" @click="generate">
            <span class="iconfont icon-robot" style="margin-right:6px"></span>生成当日日报
          </el-button>
          <span v-if="engine" style="color:var(--text-3);font-size:12.5px">
            引擎: {{ engine }} ｜ {{ ts }}<span v-if="!aiEnabled">（未配置API Key，规则引擎模式）</span>
          </span>
          <span v-else-if="!aiEnabled" style="color:var(--text-3);font-size:12.5px">
            未配置 API Key，当前为规则引擎模式（可在 ai_config.json 配置智谱 / DeepSeek Key）
          </span>
        </div>
        <div v-if="content" class="md" style="line-height:1.75" v-html="md2html(content)"></div>
        <div v-else style="color:var(--text-3);padding:24px 0;text-align:center">尚无日报，点击上方按钮生成</div>
      </div>
      <div class="panel">
        <h3 class="panel-title"><span class="iconfont icon-chat-dots" style="color:var(--accent)"></span>政策智能问答</h3>
        <div style="display:flex;gap:10px">
          <ModelSelect v-model="model" />
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

  /* ---------- 二手车大模型搜索 ---------- */
  const CarView = {
    data() {
      const now = new Date().getFullYear();
      const years = [];
      for (let y = now + 1; y >= 2005; y--) years.push(y);
      return {
        keyword: '',
        year: now - 3,          // 默认 3 年车龄（二手出口主力）
        years,
        model: '',              // AI 模型选择（空=自动容灾）
        searching: false,
        content: '',
        engine: '',
        ts: '',
        listings: [],            // 瓜子真实在售车源 [{title,reg_year,mileage,city,price,tag,url,img}]
        listingOk: false,
        listingMsg: '',          // 车源抓取说明（未识别品牌/限流提示等）
        listingSource: '',       // 车源来源描述（如"瓜子二手车·深圳站·卡罗拉"）
        history: [],            // [{id, ts, keyword, year, engine}]
        // 二手车利润计算器
        pc: {
          buy_price: '',        // 收购价（人民币，元）
          domestic_fee: 5000,   // 国内杂费（检测/整备/过户/陆运/报关，元）
          shipping_fee: 18000,  // 海运费+保险（元）
          tax_rate: 35,         // 加纳综合税负率（%）
          sell_price_ghs: '',   // 加纳当地售价（GHS）
        },
        pcResult: null,         // /api/cars/calc 返回的测算结果
        pcCalc: false,          // 利润计算中
        exporting: '',          // 'png' | 'xlsx' | '' 导出进行中
      };
    },
    methods: {
      md2html, fmt, fmtMoney,
      // 导出报告中的价格表为 PNG 图片（html2canvas 截图）
      exportTableImage() {
        const table = this.$el.querySelector('.md table');
        if (!table) { ElMessage.warning('报告中暂无表格可导出'); return; }
        this.exporting = 'png';
        const name = `${this.keyword || '车价表'}-${this.year}-价格表`;
        try {
          html2canvas(table, { backgroundColor: '#ffffff', scale: 2 })
            .then(canvas => {
              const a = document.createElement('a');
              a.download = name + '.png';
              a.href = canvas.toDataURL('image/png');
              a.click();
              ElMessage.success('价格表已导出为图片');
            })
            .catch(err => ElMessage.error('导出图片失败: ' + err.message))
            .finally(() => { this.exporting = ''; });
        } catch (e) { this.exporting = ''; ElMessage.error('导出图片失败: ' + e.message); }
      },
      // 导出报告中的价格表为 Excel（SheetJS 生成 .xlsx）
      exportTableExcel() {
        const table = this.$el.querySelector('.md table');
        if (!table) { ElMessage.warning('报告中暂无表格可导出'); return; }
        this.exporting = 'xlsx';
        const name = `${this.keyword || '车价表'}-${this.year}-价格表`;
        try {
          const ws = XLSX.utils.table_to_sheet(table);
          const wb = XLSX.utils.book_new();
          XLSX.utils.book_append_sheet(wb, ws, '价格表');
          XLSX.writeFile(wb, name + '.xlsx');
          ElMessage.success('价格表已导出为 Excel');
        } catch (e) { ElMessage.error('导出 Excel 失败: ' + e.message); }
        this.exporting = '';
      },
      // 二手车出口加纳单台利润测算（调 /api/cars/calc）
      async calcProfit() {
        if (this.pc.buy_price === '' || this.pc.sell_price_ghs === '') {
          ElMessage.warning('请填写收购价和加纳当地售价');
          return;
        }
        this.pcCalc = true;
        try {
          const d = await api('/api/cars/calc', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              buy_price: Number(this.pc.buy_price),
              domestic_fee: Number(this.pc.domestic_fee),
              shipping_fee: Number(this.pc.shipping_fee),
              tax_rate: Number(this.pc.tax_rate),
              sell_price_ghs: Number(this.pc.sell_price_ghs),
            }),
          });
          this.pcResult = d;
        } catch (e) { ElMessage.error('测算失败: ' + e.message); }
        finally { this.pcCalc = false; }
      },
      async search() {
        const kw = this.keyword.trim();
        if (!kw) { ElMessage.warning('请输入车型关键词，如：丰田卡罗拉'); return; }
        this.searching = true; this.content = ''; this.listings = []; this.listingMsg = '';
        try {
          const d = await api('/api/cars/search', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keyword: kw, year: this.year, model: this.model }),
          });
          this.content = d.content; this.engine = d.engine; this.ts = d.ts;
          this.listings = d.listings || [];
          this.listingOk = !!d.listing_ok;
          this.listingMsg = d.listing_msg || '';
          this.listingSource = d.listing_source || '';
          if (this.listings.length) ElMessage.success(`已抓取 ${this.listings.length} 条真实车源，分析完成`);
          else ElMessage.success('分析完成（未抓到真实车源）');
          this.loadHistory();
        } catch (e) { ElMessage.error('搜索失败: ' + e.message); }
        finally { this.searching = false; }
      },
      async loadHistory() {
        try {
          const d = await api('/api/cars/history');
          this.history = d.items || [];
        } catch (e) { /* 历史加载失败不阻塞 */ }
      },
      async openHistory(h) {
        try {
          const d = await api('/api/cars/' + h.id);
          this.keyword = d.keyword; this.year = d.year;
          this.content = d.content; this.engine = d.engine; this.ts = d.ts;
          this.listings = d.listings || [];
          this.listingOk = this.listings.length > 0;
          this.listingMsg = this.listings.length ? '' : '该次搜索未抓到真实车源';
          this.listingSource = this.listings.length ? '瓜子二手车·深圳站' : '';
        } catch (e) { ElMessage.error('加载失败: ' + e.message); }
      },
      async deleteHistory(h) {
        try {
          await ElMessageBox.confirm(`确定删除「${h.keyword} ${h.year} 款」这条历史记录吗？`, '删除确认', {
            confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning',
          });
        } catch (e) { return; }  // 用户取消
        try {
          await api('/api/cars/' + h.id, { method: 'DELETE' });
          this.history = this.history.filter(x => x.id !== h.id);
          ElMessage.success('已删除');
        } catch (e) { ElMessage.error('删除失败: ' + e.message); }
      },
    },
    mounted() { this.loadHistory(); },
    template: `
    <div class="content-inner">
      <div class="section-head">
        <div class="kicker">Used Car Export Research</div>
        <h2>二手车行情</h2>
        <div class="desc">输入「品牌+车型」关键词 + 年份：自动抓取瓜子二手车（深圳站，沿海港口省国内运费）真实在售车源，大模型结合真实车源分析全版本指导价 / 二手价格区间 / 版本解读 / 出口加纳建议。</div>
      </div>
      <div class="panel">
        <div class="car-search-bar">
          <el-input v-model="keyword" placeholder="车型关键词，如：丰田卡罗拉 / 本田思域 / 五菱宏光"
                   clearable size="large" @keyup.enter="search"></el-input>
          <el-select v-model="year" size="large" style="width:110px">
            <el-option v-for="y in years" :key="y" :label="y + ' 款'" :value="y"></el-option>
          </el-select>
          <ModelSelect v-model="model" />
          <el-button type="primary" size="large" :loading="searching" @click="search">
            <span class="iconfont icon-search" style="margin-right:6px"></span>大模型搜索
          </el-button>
        </div>
        <div v-if="searching" class="car-loading">
          <span class="iconfont icon-robot" style="font-size:20px;color:var(--accent)"></span>
          正在抓取瓜子二手车真实车源并调用大模型分析，约 40~120 秒，请勿关闭页面…
        </div>
        <template v-if="!searching">
          <div v-if="listings.length" class="car-source">
            <div class="car-source-head">
              <span class="iconfont icon-truck" style="color:var(--accent)"></span>
              真实在售车源 · {{ listingSource }} · 共 {{ listings.length }} 条
              <span v-if="listingMsg" class="car-warn">{{ listingMsg }}</span>
            </div>
            <div class="car-source-grid">
              <a v-for="(c, i) in listings" :key="i" :href="c.url" target="_blank" class="car-card">
                <div class="car-card-img">
                  <img v-if="c.img" :src="c.img" loading="lazy" alt="" @error="c.img=''">
                  <span v-else class="iconfont icon-truck car-card-noimg"></span>
                </div>
                <div class="car-card-title" :title="c.title">{{ c.title }}</div>
                <div class="car-card-desc">{{ c.reg_year }} · {{ c.mileage }} · {{ c.city }}</div>
                <div class="car-card-bottom">
                  <span class="car-card-price"><b>{{ c.price }}</b>万</span>
                  <span v-if="c.tag" class="car-card-tag">{{ c.tag }}</span>
                </div>
              </a>
            </div>
          </div>
          <div v-else-if="listingMsg" class="car-warn-box">
            <span class="iconfont icon-truck" style="margin-right:6px"></span>{{ listingMsg }}
          </div>
          <div v-if="content">
            <div class="car-meta">
              <span class="iconfont icon-robot" style="color:var(--accent)"></span>
              引擎: {{ engine }} ｜ 生成于 {{ ts }}
              <span class="car-warn">（未标注"基于真实车源"的部分为模型知识，成交前须实车核价）</span>
              <span class="car-meta-actions">
                <el-button size="small" :loading="exporting==='png'" @click="exportTableImage">
                  <span class="iconfont icon-image" style="margin-right:4px"></span>导出图片
                </el-button>
                <el-button size="small" :loading="exporting==='xlsx'" @click="exportTableExcel">
                  <span class="iconfont icon-file-earmark-excel" style="margin-right:4px"></span>导出 Excel
                </el-button>
              </span>
            </div>
            <div class="md-table-wrap">
              <div class="md" style="line-height:1.75" v-html="md2html(content)"></div>
            </div>
          </div>
          <div v-else style="color:var(--text-3);padding:24px 0;text-align:center">
            输入车型关键词开始检索，历史搜索将自动保存
          </div>
        </template>
      </div>
      <div class="panel">
        <h3 class="panel-title">
          <span class="iconfont icon-calculator" style="color:var(--accent)"></span>二手车利润计算器
        </h3>
        <div class="car-calc-formula">
          <span class="iconfont icon-info-circle" style="margin-right:4px"></span>
          参考公式：单台利润 = 加纳当地售价(折人民币) − [收购价 + 国内杂费 + 海运费] × (1 + 加纳综合税负率)
        </div>
        <div class="car-calc-form">
          <div class="car-calc-field">
            <label>收购价（元）</label>
            <el-input v-model="pc.buy_price" placeholder="如 70000" @keyup.enter="calcProfit"></el-input>
          </div>
          <div class="car-calc-field">
            <label>国内杂费（元）</label>
            <el-input v-model="pc.domestic_fee" placeholder="检测/整备/过户/陆运/报关"></el-input>
          </div>
          <div class="car-calc-field">
            <label>海运费（元）</label>
            <el-input v-model="pc.shipping_fee" placeholder="海运费 + 保险"></el-input>
          </div>
          <div class="car-calc-field">
            <label>加纳综合税负率（%）</label>
            <el-input v-model="pc.tax_rate" placeholder="默认 35"></el-input>
          </div>
          <div class="car-calc-field">
            <label>加纳当地售价（GHS）</label>
            <el-input v-model="pc.sell_price_ghs" placeholder="如 150000" @keyup.enter="calcProfit"></el-input>
          </div>
          <div class="car-calc-field car-calc-field-btn">
            <el-button type="primary" :loading="pcCalc" @click="calcProfit" style="width:100%">
              <span class="iconfont icon-calculator" style="margin-right:4px"></span>测算利润
            </el-button>
          </div>
        </div>
        <div v-if="pcResult" class="car-calc-result">
          <div class="car-calc-kpis">
            <div class="car-calc-kpi"><span>到岸成本 CIF</span><b>{{ fmtMoney(pcResult.cif) }}</b></div>
            <div class="car-calc-kpi"><span>落地总成本</span><b>{{ fmtMoney(pcResult.total_cost) }}</b></div>
            <div class="car-calc-kpi"><span>加纳售价(折人民币)</span><b>{{ fmtMoney(pcResult.sell_price_cny) }}</b></div>
            <div class="car-calc-kpi car-calc-kpi-profit">
              <span>单台利润</span>
              <b :class="pcResult.profit >= 0 ? 'car-profit-pos' : 'car-profit-neg'">{{ fmtMoney(pcResult.profit) }}</b>
            </div>
          </div>
          <div class="car-calc-detail">
            <span>利润率 <b :class="pcResult.profit >= 0 ? 'car-profit-pos' : 'car-profit-neg'">{{ pcResult.profit_rate }}%</b></span>
            <span>加纳税费 {{ fmtMoney(pcResult.tax_amount) }}（{{ pcResult.tax_rate }}%）</span>
            <span>汇率 1 GHS ≈ {{ pcResult.ghs_cny }} 元（USD/CNY {{ pcResult.usd_cny }} · USD/GHS {{ pcResult.usd_ghs }}）</span>
          </div>
        </div>
      </div>
      <div class="panel" v-if="history.length">
        <h3 class="panel-title"><span class="iconfont icon-clock" style="color:var(--accent)"></span>历史搜索</h3>
        <div class="car-history">
          <div v-for="h in history" :key="h.id" class="car-history-item" @click="openHistory(h)">
            <span class="car-history-kw">{{ h.keyword }}</span>
            <span class="car-history-year">{{ h.year }} 款</span>
            <span class="car-history-ts">{{ h.ts }}</span>
            <button class="car-history-del" @click.stop="deleteHistory(h)" title="删除该记录">
              <span class="iconfont icon-trash"></span>
            </button>
          </div>
        </div>
      </div>
    </div>`,
  };

  /* ---------- 合同审核 ---------- */
  const ContractView = {
    data() {
      return {
        title: '',
        content: '',
        type: 'sales',          // 合同类型：sales/purchase/agency/freight/nda
        types: [                // 类型清单（后端 /api/contract/template 会刷新）
          { id: 'sales', zh: '销售合同', en: 'Sales Contract' },
          { id: 'purchase', zh: '采购合同', en: 'Purchase Contract' },
          { id: 'agency', zh: '独家代理协议', en: 'Agency Agreement' },
          { id: 'freight', zh: '货运代理协议', en: 'Freight Forwarding Agreement' },
          { id: 'nda', zh: '保密协议', en: 'Non-Disclosure Agreement' },
        ],
        curLabels: { zh: '销售合同', en: 'Sales Contract' },  // 当前类型双语名称（标题前缀用）
        lang: 'zh',             // 'zh' | 'en' 双语模板切换
        pristine: true,          // 内容是否仍是未修改的模板（切换语言/类型时自动互换）
        templates: { zh: '', en: '' },
        downloading: false,
        reviewing: false,
        model: '',              // AI 模型选择（空=DeepSeek 优先）
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
          const d = await api('/api/contract/template?type=' + this.type + '&lang=' + this.lang);
          if (d.types && d.types.length) this.types = d.types;
          this.curLabels = { zh: d.zh_label || '销售合同', en: d.en_label || 'Sales Contract' };
          this.templates = { zh: d.zh || '', en: d.en || d.template || '' };
          this.pristine = true;
          this.content = this.templates[this.lang] || '';
          this.title = this.titlePrefix(this.lang)
            + new Date().toISOString().slice(0, 10).replace(/-/g, '');
          ElMessage.success('已加载' + this.curLabels[this.lang]
            + '模板（' + (this.lang === 'zh' ? '中文版' : '英文版') + '）');
        } catch (e) { ElMessage.error('加载模板失败: ' + e.message); }
      },
      titlePrefix(lang) {
        const name = this.curLabels[lang] || '';
        return (lang === 'zh' ? name : name.replace(/\s+/g, '')) + '_';
      },
      switchType(t) {
        if (t === this.type) return;
        this.type = t;
        // 内容未被编辑过（仍是模板原文）→ 直接载入新类型模板
        if (this.pristine) {
          this.loadTemplate();
        } else {
          ElMessage.info('已切换合同类型（当前内容已编辑，保留原文；点击「加载模板」可载入新类型模板）');
        }
      },
      switchLang(lang) {
        if (lang === this.lang) return;
        // 内容未被编辑过（仍是模板原文）→ 直接切换到对应语言模板
        if (this.pristine && this.templates[lang]) {
          this.lang = lang;
          this.content = this.templates[lang];
          this.title = this.titlePrefix(lang)
            + new Date().toISOString().slice(0, 10).replace(/-/g, '');
        } else {
          this.lang = lang;
          ElMessage.info('已切换语言（当前内容已编辑，保留原文；点击「加载模板」可重置为'
            + (lang === 'zh' ? '中文' : '英文') + '模板）');
        }
      },
      async downloadPdf() {
        if (!this.content.trim()) { ElMessage.warning('合同内容为空，无法导出 PDF'); return; }
        this.downloading = true;
        try {
          const res = await fetch('/api/contract/pdf', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: this.title, content: this.content, lang: this.lang }),
          });
          if (!res.ok) {
            let msg = 'HTTP ' + res.status;
            try { msg = (await res.json()).error || msg; } catch (_) {}
            throw new Error(msg);
          }
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url;
          a.download = (this.title || 'contract').replace(/[\\/:*?"<>|]/g, '_') + '.pdf';
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 3000);
          ElMessage.success('PDF 已生成并开始下载');
        } catch (e) { ElMessage.error('PDF 导出失败: ' + e.message); }
        finally { this.downloading = false; }
      },
      async doReview() {
        if (!this.canReview) { ElMessage.warning('合同内容太短，请输入完整合同文本'); return; }
        this.reviewing = true;
        this.result = null;
        try {
          this.result = await api('/api/contract/review', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: this.title, content: this.content, model: this.model }),
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
          this.pristine = false;   // 历史合同内容 ≠ 模板，切换语言不再自动替换
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
        <div class="desc">多种合同模板（销售/采购/独家代理/货运代理/保密协议），每种均有中/英文两版，一键导出 PDF；AI 审核贸易合同，逐条检查经营红线、付款条款、合规认证等（默认优先使用 DeepSeek 引擎，多通道容灾）。</div>
      </div>
      <div class="contract-layout">
        <div class="contract-editor panel">
          <h3 class="panel-title"><span class="iconfont icon-file-earmark-text" style="color:var(--accent)"></span>合同文本
            <span class="tip">编辑或粘贴合同全文</span></h3>
          <div class="toolbar" style="margin-bottom:10px;flex-wrap:wrap;gap:6px">
            <el-select v-model="type" style="width:180px" @change="switchType">
              <el-option v-for="t in types" :key="t.id" :value="t.id" :label="t.zh"></el-option>
            </el-select>
            <el-input v-model="title" :placeholder="lang === 'zh' ? '合同标题' : 'Contract Title'" style="width:240px"></el-input>
            <el-button-group style="margin:0 2px">
              <el-button :type="lang === 'zh' ? 'primary' : ''" size="small" @click="switchLang('zh')">中文版</el-button>
              <el-button :type="lang === 'en' ? 'primary' : ''" size="small" @click="switchLang('en')">English</el-button>
            </el-button-group>
            <el-button @click="loadTemplate">
              <span class="iconfont icon-file-earmark-plus" style="margin-right:6px"></span>加载模板
            </el-button>
            <el-button @click="downloadPdf" :loading="downloading">
              <span class="iconfont icon-download" style="margin-right:6px"></span>下载 PDF
            </el-button>
            <ModelSelect v-model="model" />
            <el-button type="primary" :loading="reviewing" :disabled="!canReview" @click="doReview">
              <span class="iconfont icon-magic" style="margin-right:6px"></span>AI 审核
            </el-button>
            <el-button text @click="toggleHistory">
              {{ showHistory ? '收起' : '查看' }}审核历史（{{ history.length }}）
            </el-button>
          </div>
          <el-input v-model="content" type="textarea" :rows="22" @input="pristine = false"
            placeholder="在此粘贴或编辑合同全文…可在上方选择合同类型，点击"加载模板"使用中/英文标准模板"
            style="font-family:monospace;font-size:12.5px"></el-input>
        </div>
        <div class="contract-result panel">
          <h3 class="panel-title">
            <span class="iconfont icon-shield-check" style="color:var(--accent)"></span>审核结果
            <span v-if="result" class="engine-badge" :title="'审核引擎：' + result.engine">{{ result.engine }}</span>
          </h3>
          <div v-if="reviewing" style="color:var(--text-3);padding:30px 0;text-align:center">
            <span class="iconfont icon-arrow-clockwise" style="font-size:24px;animation:spin 1s linear infinite;display:inline-block"></span>
            <p style="margin-top:10px">AI 正在审核合同，约 10-30 秒…</p>
          </div>
          <div v-else-if="result" style="overflow:hidden">
            <div class="risk-banner" :style="{borderColor: riskColor}">
              <span class="risk-label" :style="{color: riskColor}">{{ riskLabel }}</span>
              <span style="color:var(--text-3);font-size:12.5px">风险扣分 {{ 100 - (result.risk_score || 0) }}/100</span>
              <span style="color:var(--text-3);font-size:12.5px"> ｜ {{ result.ts }}</span>
            </div>
            <div class="md contract-review-md" v-html="md2html(result.review)"></div>
          </div>
          <div v-else style="color:var(--text-3);padding:30px 0;text-align:center">
            点击「AI 审核」按钮，开始审核合同文本
          </div>
        </div>
      </div>
      <div v-if="showHistory && history.length" class="panel" v-loading="loadingHistory">
        <h3 class="panel-title"><span class="iconfont icon-clock" style="color:var(--accent)"></span>审核历史</h3>
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
            <template #default="{ row }"><span style="color:var(--text-3)">{{ fmtDate(row.created_at) }}</span></template>
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
        aiModel: '',           // AI 模型选择（空=自动容灾）
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
              result: this.result, model: this.aiModel,
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
        <h3 class="panel-title"><span class="iconfont icon-box-seam" style="color:var(--accent)"></span>商品清单
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
        <h3 class="panel-title"><span class="iconfont icon-truck" style="color:var(--accent)"></span>海运物流费</h3>
        <el-radio-group v-model="freightMode" style="margin-bottom:12px">
          <el-radio value="auto">按总重量自动估算</el-radio>
          <el-radio value="fixed">固定金额</el-radio>
        </el-radio-group>
        <div v-if="freightMode === 'auto'" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px">
          <div><div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">基础费（元）</div>
            <el-input-number v-model="params.freight_base" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">每公斤海运费（元/kg）</div>
            <el-input-number v-model="params.freight_per_kg" :controls="false" :min="0" :precision="2" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">预估海运物流费</div>
            <el-input :model-value="result ? fmtMoney(result.freight) : '—'" readonly
                      style="font-weight:600;color:var(--text)"></el-input></div>
        </div>
        <div v-else style="max-width:260px">
          <div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">海运物流费（元）</div>
          <el-input-number v-model="params.freight" :controls="false" :min="0" style="width:100%"></el-input-number>
        </div>
      </div>
      <div class="panel">
        <h3 class="panel-title"><span class="iconfont icon-cash-coin" style="color:var(--accent)"></span>财务与费用参数</h3>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px">
          <div><div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">报关代理费（元）</div>
            <el-input-number v-model="params.agent_fee" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">寄样验货杂费（元）</div>
            <el-input-number v-model="params.misc_fee" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">加价率（%）</div>
            <el-input-number v-model="params.markup" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">定金比例（%）</div>
            <el-input-number v-model="params.deposit_ratio" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">加纳综合税负（%，未选品类时用）</div>
            <el-input-number v-model="params.ghana_tax" :controls="false" :min="0" style="width:100%"></el-input-number></div>
          <div><div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">重庆合伙人股比（%）</div>
            <el-input-number v-model="params.equity_a" :controls="false" :min="0" :max="100" style="width:100%"></el-input-number></div>
        </div>
      </div>
      <div class="panel" v-loading="loading">
        <h3 class="panel-title"><span class="iconfont icon-currency-exchange" style="color:var(--accent)"></span>测算结果</h3>
        <template v-if="result">
          <div class="calc-sub">商品明细（含分品类税负）</div>
          <el-table :data="result.items" size="small" style="width:100%;margin-bottom:8px">
            <el-table-column prop="name" label="商品" min-width="120"></el-table-column>
            <el-table-column label="品类" min-width="100">
              <template #default="{ row }">
                <span v-if="row.category_name">{{ row.category_name }}</span>
                <span v-else style="color:var(--text-3)">未分类</span>
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
                <span style="font-weight:600;color:var(--red-strong)">{{ row.ghana_tax_pct }}%</span>
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
        <div v-else style="color:var(--text-3);padding:18px 0;text-align:center">正在计算…</div>
      </div>
      <div class="panel">
        <h3 class="panel-title">
          <span class="iconfont icon-robot" style="color:var(--accent)"></span>报价操作：保存 / AI预测对比
        </h3>
        <div class="toolbar" style="margin-bottom:14px">
          <el-button type="primary" @click="openSaveDialog" :disabled="!result">
            <span class="iconfont icon-floppy-disk" style="margin-right:6px"></span>保存报价单
          </el-button>
          <el-button @click="doAiPredict" :loading="aiPredicting" :disabled="!result">
            <span class="iconfont icon-graph-up" style="margin-right:6px"></span>AI预测对比
          </el-button>
          <ModelSelect v-model="aiModel" />
          <el-button text @click="showQuoteList = !showQuoteList">
            {{ showQuoteList ? '收起' : '查看' }}报价历史（{{ quotes.length }}）
          </el-button>
        </div>
        <!-- AI 预测对比面板 -->
        <div v-if="aiPrediction" class="ai-compare-panel">
          <div class="calc-sub" style="margin-bottom:10px">
            <span class="iconfont icon-graph-up" style="color:var(--accent);margin-right:4px"></span>
            AI vs 人工报价对比
            <span class="tag" style="background:#eef2ff;color:var(--accent);margin-left:8px">{{ aiPrediction.engine }}</span>
          </div>
          <div class="compare-grid">
            <div class="compare-col">
              <div class="compare-header" style="color:var(--text)">人工报价</div>
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
          <div v-else class="compare-diff" style="color:var(--text-3)">AI与人工报价一致</div>
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
          <div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">报价单名称</div>
          <el-input v-model="quoteName" placeholder="如：首单摩配5SKU"></el-input>
        </div>
        <div>
          <div class="label" style="font-size:12px;color:var(--text-3);margin-bottom:4px">备注</div>
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
    components: { OverviewView, RatesView, PoliciesView, PartsView, AiReportView, CarView, ContractView, CalculatorView },
    data() {
      return {
        activeView: 'overview',
        theme: themeStore,
        me: null,
        summary: { latest_rate: null, rates: [], ghs_change_pct: null, policies: [], fetch_log: [],
                   calc_defaults: {}, calc_default_item: {}, categories: [], ai_enabled: false, last_report: null },
        loading: true,
        error: '',
        settingsVisible: false,
        settingsSaving: false,
        settingsForm: { account: '', username: '', email: '', phone: '', old_password: '', password: '', confirm: '' },
      };
    },
    computed: {
      themeIcon() {
        return this.theme.mode === 'dark' ? 'icon-moon-stars'
          : this.theme.mode === 'light' ? 'icon-sun' : 'icon-circle-half';
      },
      themeTitle() {
        const names = { auto: '跟随系统（当前' + (effectiveTheme() === 'dark' ? '夜晚' : '白天') + '）',
                        light: '白天', dark: '夜晚' };
        const next = { auto: '白天', light: '夜晚', dark: '跟随系统' }[this.theme.mode];
        return '主题：' + names[this.theme.mode] + '，点击切换为「' + next + '」';
      },
      dateStr() {
        const t = new Date();
        return t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0') + '-' + String(t.getDate()).padStart(2, '0');
      },
      rateText() {
        const r = this.summary.latest_rate;
        return r ? `USD/CNY ${r.usd_cny.toFixed(4)}｜USD/GHS ${r.usd_ghs.toFixed(4)}` : '暂无汇率数据';
      },
      rateTip() { return '最新汇率：USD/CNY 美元兑人民币 ｜ USD/GHS 美元兑加纳塞地'; },
      // 当前激活视图对应的组件名（供 keep-alive 动态切换，切换不销毁组件、后台任务不中断）
      activeComponent() {
        const map = {
          overview: 'OverviewView', rates: 'RatesView', policies: 'PoliciesView',
          parts: 'PartsView', ai: 'AiReportView', cars: 'CarView',
          contract: 'ContractView', calculator: 'CalculatorView',
        };
        return map[this.activeView] || 'OverviewView';
      },
      // 各视图所需的 props（按 activeView 动态注入）
      viewProps() {
        const s = this.summary;
        switch (this.activeView) {
          case 'overview': return { summary: s };
          case 'rates': return { rates: s.rates, latest: s.latest_rate };
          case 'policies': return { policies: s.policies };
          case 'ai': return { lastReport: s.last_report, aiEnabled: s.ai_enabled };
          case 'calculator': return { defaults: s.calc_defaults, defaultItem: s.calc_default_item, categories: s.categories };
          default: return {};
        }
      },
    },
    methods: {
      async loadMe() {
        try {
          this.me = await api('/api/auth/me');
        } catch (e) {
          // 未登录 → 跳转到登录页
          window.location.href = '/login.html';
        }
      },
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
      goLogin() { window.location.href = '/login.html'; },
      onUserCommand(cmd) {
        if (cmd === 'logout') {
          api('/api/auth/logout').finally(() => { window.location.href = '/login.html'; });
        } else if (cmd === 'settings') {
          this.openSettings();
        } else if (cmd === 'realname') {
          ElMessage.info('实名认证（预留支付宝实名 API）即将上线');
        }
      },
      openSettings() {
        const m = this.me || {};
        this.settingsForm = {
          account: m.account || '', username: m.username || '',
          email: m.email || '', phone: m.phone || '',
          old_password: '', password: '', confirm: '',
        };
        this.settingsVisible = true;
      },
      async saveSettings() {
        const f = this.settingsForm;
        if (!f.username) { ElMessage.warning('用户名不能为空'); return; }
        if (f.email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(f.email)) { ElMessage.warning('邮箱格式不正确'); return; }
        if (f.phone && !/^1\d{10}$/.test(f.phone)) { ElMessage.warning('手机号格式不正确'); return; }
        const payload = { username: f.username, email: f.email, phone: f.phone };
        // 仅当填写新密码时才发起改密（须当前密码验证身份，且不可与最近 5 次相同）
        if (f.password) {
          if (!f.old_password) { ElMessage.warning('修改密码需先填写当前密码以验证身份'); return; }
          if (f.password.length < 8 || !/[A-Za-z]/.test(f.password) || !/\d/.test(f.password)) {
            ElMessage.warning('新密码至少 8 位，且同时包含字母和数字'); return;
          }
          if (f.password !== f.confirm) { ElMessage.warning('两次输入的新密码不一致'); return; }
          payload.old_password = f.old_password;
          payload.password = f.password;
        }
        this.settingsSaving = true;
        try {
          const r = await api('/api/auth/update', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          });
          this.me = r.user;
          this.settingsForm.old_password = '';
          this.settingsForm.password = '';
          this.settingsForm.confirm = '';
          ElMessage.success('已保存');
          this.settingsVisible = false;
        } catch (e) {
          ElMessage.error(e.message || '保存失败');
        } finally { this.settingsSaving = false; }
      },
      cycleTheme,
    },
    mounted() { this.loadMe(); this.loadSummary(); loadAiModels(); },
    template: `
    <el-container class="app-shell">
      <el-header class="topbar" height="52px">
        <div class="brand">
          <span class="logo"><span class="iconfont icon-globe2"></span></span>
          <span>中加贸易智能分析</span>
        </div>
        <div class="top-right">
          <span class="rate-status" :title="rateTip">{{ rateText }}</span>
          <el-dropdown v-if="me" trigger="click" @command="onUserCommand">
            <span class="nav-user">
              <span class="nav-avatar"><span class="iconfont icon-person-circle"></span></span>
              <span class="nav-uname">{{ me.username }}</span>
              <span class="iconfont icon-caret-down" style="font-size:11px;opacity:.6"></span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="settings"><span class="iconfont icon-gear" style="margin-right:6px"></span>账号设置</el-dropdown-item>
                <el-dropdown-item command="realname"><span class="iconfont icon-shield-check" style="margin-right:6px"></span>实名认证</el-dropdown-item>
                <el-dropdown-item command="logout" divided><span class="iconfont icon-box-arrow-right" style="margin-right:6px"></span>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
          <span v-else class="nav-icon" title="登录" @click="goLogin"><span class="iconfont icon-person-circle"></span></span>
          <span class="nav-icon theme-toggle" :title="themeTitle" @click="cycleTheme">
            <span class="iconfont" :class="themeIcon"></span>
          </span>
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
            <el-menu-item index="cars"><span class="iconfont icon-truck menu-ico"></span>二手车</el-menu-item>
            <el-menu-item index="contract"><span class="iconfont icon-file-earmark-text menu-ico"></span>合同审核</el-menu-item>
            <el-menu-item index="calculator"><span class="iconfont icon-cash-coin menu-ico"></span>利润测算</el-menu-item>
          </el-menu>
          <div class="sidebar-foot">
            <div><span class="iconfont icon-database" style="margin-right:4px"></span>数据：汇率 / 政策 / 配件 / 商品库</div>
            <div><span class="iconfont icon-shield-check" style="margin-right:4px"></span>经营红线：先收定金再采购</div>
          </div>
        </el-aside>
        <el-main class="content" v-loading="loading">
          <keep-alive>
            <component :is="activeComponent" v-bind="viewProps" @refresh="loadSummary" />
          </keep-alive>
        </el-main>
      </el-container>
      <el-dialog v-model="settingsVisible" title="账号设置" width="460px" :close-on-click-modal="false" append-to-body>
        <el-form label-width="96px" label-position="right" :model="settingsForm" @submit.prevent>
          <div class="settings-section-title">基本资料</div>
          <el-form-item label="账号">
            <el-input :model-value="settingsForm.account" disabled></el-input>
          </el-form-item>
          <el-form-item label="用户名">
            <el-input v-model="settingsForm.username" placeholder="昵称"></el-input>
          </el-form-item>
          <el-form-item label="邮箱">
            <el-input v-model="settingsForm.email" placeholder="name@example.com"></el-input>
          </el-form-item>
          <el-form-item label="手机号">
            <el-input v-model="settingsForm.phone" placeholder="11 位手机号"></el-input>
          </el-form-item>
          <div class="settings-section-title">
            修改密码
            <span class="settings-hint">（需当前密码验证身份，且不能与前 5 次使用过的密码相同）</span>
          </div>
          <el-form-item label="当前密码">
            <el-input v-model="settingsForm.old_password" type="password" show-password placeholder="留空则不修改密码"></el-input>
          </el-form-item>
          <el-form-item label="新密码">
            <el-input v-model="settingsForm.password" type="password" show-password placeholder="≥8 位，字母+数字"></el-input>
          </el-form-item>
          <el-form-item label="确认新密码">
            <el-input v-model="settingsForm.confirm" type="password" show-password></el-input>
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="settingsVisible = false">取消</el-button>
          <el-button type="primary" :loading="settingsSaving" @click="saveSettings">保存</el-button>
        </template>
      </el-dialog>
    </el-container>`,
  };

  /* ---------------- 启动 ---------------- */
  const app = createApp(App);
  for (const [name, comp] of Object.entries(ElementPlusIconsVue)) {
    app.component(name, comp);
  }
  app.component('ModelSelect', ModelSelect);   // 全局注册 AI 模型选择器
  app.use(ElementPlus);
  app.mount('#app');
})();
