/* 登录 / 注册页逻辑（Vue3 全局构建，无打包） */
(function () {
  'use strict';
  const { createApp, reactive, toRefs, onMounted } = Vue;

  // 真实 4K 风景图（本地托管于 web/bg/，离线可用；来自 Unsplash 公共授权图库）
  const BG_IMGS = [
    '/bg/landscape1.jpg', // 山湖
    '/bg/landscape2.jpg', // 雪山湖泊
    '/bg/landscape3.jpg', // 瀑布峡谷
    '/bg/landscape4.jpg', // 雪峰
  ];

  // 背景幻灯片：注入 <img> 并交叉淡入
  function initBg() {
    const stage = document.getElementById('bgStage');
    if (!stage) return;
    BG_IMGS.forEach((src, i) => {
      const img = new Image();
      img.className = 'bg-img' + (i === 0 ? ' active' : '');
      img.src = src;
      img.alt = '';
      stage.insertBefore(img, stage.firstChild); // 压在 overlay 之下
    });
    let idx = 0;
    setInterval(() => {
      const imgs = stage.querySelectorAll('.bg-img');
      if (imgs.length < 2) return;
      imgs[idx].classList.remove('active');
      idx = (idx + 1) % imgs.length;
      imgs[idx].classList.add('active');
    }, 7000);
  }

  async function api(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await res.json(); } catch (e) {}
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  createApp({
    setup() {
      const state = reactive({
        tab: 'login',
        account: '', username: '', email: '', phone: '',
        password: '', code: '',
        loading: false,
        cooling: 0,
        msg: '', msgType: 'ok',
        captchaSiteKey: '',   // 非空时显示 Turnstile 验证框
        captchaToken: '',     // 当前人机验证令牌
        _turnstileRendered: false,
      });
      let timer = null;

      function setMsg(text, type) { state.msg = text; state.msgType = type || 'ok'; }

      function startCooldown() {
        state.cooling = 60;
        timer = setInterval(() => {
          state.cooling -= 1;
          if (state.cooling <= 0) { clearInterval(timer); state.cooling = 0; }
        }, 1000);
      }

      // ---- 人机验证（Cloudflare Turnstile，免费）----
      async function loadCaptcha() {
        try {
          const d = await fetch('/api/auth/captcha-config').then(r => r.json());
          if (d.site_key) {
            state.captchaSiteKey = d.site_key;
            loadTurnstileScript(d.site_key);
          }
        } catch (e) { /* dev 模式无此配置也可正常登录 */ }
      }
      function loadTurnstileScript(sitekey) {
        if (window.turnstile) { renderTurnstile(sitekey); return; }
        const s = document.createElement('script');
        s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
        s.async = true; s.defer = true;
        s.onload = () => renderTurnstile(sitekey);
        document.head.appendChild(s);
      }
      function renderTurnstile(sitekey) {
        if (!window.turnstile || state._turnstileRendered) return;
        state._turnstileRendered = true;
        window.turnstile.render('#cf-captcha', {
          sitekey: sitekey,
          callback: (t) => { state.captchaToken = t; },
          'expired-callback': () => { state.captchaToken = ''; },
          'error-callback': () => { state.captchaToken = ''; },
        });
      }

      async function sendCode() {
        if (state.cooling > 0) return;
        if (state.captchaSiteKey && !state.captchaToken) {
          setMsg('请先完成人机验证', 'err'); return;
        }
        const target = state.email.trim();
        if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(target)) {
          setMsg('请先填写正确的邮箱', 'err'); return;
        }
        try {
          const d = await api('/api/auth/send-code', {
            channel: 'email', target, purpose: 'register', captcha: state.captchaToken,
          });
          if (d.dev) {
            setMsg('开发模式：验证码为 ' + d.code + '（配置邮件 API 后将以邮件发送）', 'dev');
          } else {
            setMsg('验证码已发送至 ' + target, 'ok');
          }
          startCooldown();
        } catch (e) {
          setMsg(e.message, 'err');
        }
      }

      async function login() {
        if (!state.account || !state.password) { setMsg('请输入账号和密码', 'err'); return; }
        if (state.captchaSiteKey && !state.captchaToken) {
          setMsg('请先完成人机验证', 'err'); return;
        }
        state.loading = true; setMsg('');
        try {
          await api('/api/auth/login', {
            account: state.account, password: state.password, captcha: state.captchaToken,
          });
          window.location.href = '/';
        } catch (e) {
          setMsg(e.message, 'err');
        } finally { state.loading = false; }
      }

      async function register() {
        if (state.password.length < 8) { setMsg('密码至少 8 位', 'err'); return; }
        if (!state.code) { setMsg('请填写验证码', 'err'); return; }
        if (state.captchaSiteKey && !state.captchaToken) {
          setMsg('请先完成人机验证', 'err'); return;
        }
        state.loading = true; setMsg('');
        try {
          const d = await api('/api/auth/register', {
            username: state.username,
            email: state.email, phone: state.phone,
            password: state.password, code: state.code, captcha: state.captchaToken,
          });
          // 注册成功后账号由系统自动生成（11 位易记号码），展示并复制方便记忆
          const acc = d.account || (d.user && d.user.account) || '';
          setMsg('注册成功！您的登录账号为：' + acc + '（已自动复制，请牢记）', 'ok');
          try { if (navigator.clipboard) navigator.clipboard.writeText(acc); } catch (e) {}
          setTimeout(() => { window.location.href = '/'; }, 2600);
        } catch (e) {
          setMsg(e.message, 'err');
        } finally { state.loading = false; }
      }

      onMounted(() => {
        initBg();
        loadCaptcha();
        // 鼠标/键盘聚焦中间模块 → 背景虚化 + 卡片放大
        const el = document.getElementById('auth');
        const on = () => document.body.classList.add('bg-focus');
        const off = () => document.body.classList.remove('bg-focus');
        el.addEventListener('mouseenter', on);
        el.addEventListener('mouseleave', off);
        el.addEventListener('focusin', on);
        el.addEventListener('focusout', off);
      });

      return { ...toRefs(state), sendCode, login, register };
    },
  }).mount('#auth');
})();
