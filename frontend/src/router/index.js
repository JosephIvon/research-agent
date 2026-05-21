import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      name: 'home',
      component: () => import('../views/HomeView.vue')
    },
    {
      path: '/progress/:id',
      name: 'progress',
      component: () => import('../views/ProgressView.vue')
    },
    {
      path: '/report/:id',
      name: 'report',
      component: () => import('../views/ReportView.vue')
    },
    {
      path: '/followup/:id',
      name: 'followup',
      component: () => import('../views/FollowupView.vue')
    },
    {
      path: '/review/:id',
      name: 'review',
      component: () => import('../views/ReviewView.vue')
    },
    {
      path: '/reports',
      name: 'reports',
      component: () => import('../views/HistoryView.vue')
    },
    {
      path: '/settings',
      name: 'settings',
      component: () => import('../views/SettingsView.vue')
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/LoginView.vue')
    },
    {
      path: '/sync',
      name: 'sync',
      component: () => import('../views/SyncView.vue')
    }
  ]
})

export default router
