/* 登录 / 注册页逻辑（Vue3 全局构建，无打包） */
(function () {
  'use strict';
  const { createApp, reactive, toRefs, ref } = Vue;

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

      async function sendCode() {
        if (state.cooling > 0) return;
        const target = state.email.trim();
        if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(target)) {
          setMsg('请先填写正确的邮箱', 'err'); return;
        }
        try {
          const d = await api('/api/auth/send-code', { channel: 'email', target, purpose: 'register' });
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
        state.loading = true; setMsg('');
        try {
          await api('/api/auth/login', { account: state.account, password: state.password });
          window.location.href = '/';
        } catch (e) {
          setMsg(e.message, 'err');
        } finally { state.loading = false; }
      }

      async function register() {
        if (state.password.length < 8) { setMsg('密码至少 8 位', 'err'); return; }
        if (!state.code) { setMsg('请填写验证码', 'err'); return; }
        state.loading = true; setMsg('');
        try {
          await api('/api/auth/register', {
            account: state.account, username: state.username,
            email: state.email, phone: state.phone,
            password: state.password, code: state.code,
          });
          setMsg('注册成功，正在登录…', 'ok');
          setTimeout(() => { window.location.href = '/'; }, 600);
        } catch (e) {
          setMsg(e.message, 'err');
        } finally { state.loading = false; }
      }

      return { ...toRefs(state), sendCode, login, register };
    },
  }).mount('#auth');
})();
