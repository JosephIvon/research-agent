<template>
  <main class="login-page">
    <section class="login-panel">
      <div class="brand">
        <h1>竞品调研工作台</h1>
        <p>登录后开始调研、查看报告和同步文档。</p>
      </div>

      <el-tabs v-model="mode" stretch>
        <el-tab-pane label="登录" name="login">
          <el-form :model="loginForm" label-position="top" @submit.prevent>
            <el-form-item label="用户名">
              <el-input v-model="loginForm.username" autocomplete="username" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="loginForm.password" type="password" show-password autocomplete="current-password" />
            </el-form-item>
            <el-button type="primary" class="submit" :loading="loading" @click="login">登录</el-button>
          </el-form>
        </el-tab-pane>

        <el-tab-pane label="注册" name="register">
          <el-form :model="registerForm" label-position="top" @submit.prevent>
            <el-form-item label="用户名">
              <el-input v-model="registerForm.username" autocomplete="username" />
            </el-form-item>
            <el-form-item label="邮箱">
              <el-input v-model="registerForm.email" autocomplete="email" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="registerForm.password" type="password" show-password autocomplete="new-password" />
            </el-form-item>
            <el-button type="primary" class="submit" :loading="loading" @click="register">创建账号</el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>
    </section>
  </main>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { authApi } from '../api'

const route = useRoute()
const router = useRouter()
const mode = ref('login')
const loading = ref(false)

const loginForm = reactive({
  username: '',
  password: ''
})

const registerForm = reactive({
  username: '',
  email: '',
  password: ''
})

const redirectPath = computed(() => {
  const redirect = route.query.redirect
  return typeof redirect === 'string' && redirect.startsWith('/') ? redirect : '/'
})

function persistToken(data) {
  sessionStorage.setItem('auth_token', data.access_token)
  ElMessage.success('已登录')
  router.replace(redirectPath.value)
}

async function login() {
  if (!loginForm.username || !loginForm.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    const data = await authApi.login(loginForm)
    persistToken(data)
  } finally {
    loading.value = false
  }
}

async function register() {
  if (!registerForm.username || !registerForm.email || !registerForm.password) {
    ElMessage.warning('请完整填写注册信息')
    return
  }
  loading.value = true
  try {
    const data = await authApi.register(registerForm)
    persistToken(data)
  } finally {
    loading.value = false
  }
}
</script>

<style lang="scss" scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 32px 16px;
  background: var(--bg-page);
}

.login-panel {
  width: min(440px, 100%);
  padding: 32px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-card);
  box-shadow: var(--shadow-sm);
}

.brand {
  margin-bottom: 24px;
  text-align: center;

  h1 {
    margin: 0 0 8px;
    font-size: 26px;
    line-height: 1.25;
    color: var(--text-primary);
  }

  p {
    margin: 0;
    color: var(--text-secondary);
  }
}

.submit {
  width: 100%;
  margin-top: 8px;
}
</style>
