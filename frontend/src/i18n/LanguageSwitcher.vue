<script setup>
import { useI18n } from './index';
import { ref } from 'vue';
import { ArrowDown } from '@element-plus/icons-vue';

const { locale, t } = useI18n();
const showDropdown = ref(false);

function switchLanguage(lang) {
  locale.value = lang;
  localStorage.setItem('locale', lang);
  showDropdown.value = false;
}
</script>

<template>
  <div class="language-switcher">
    <el-dropdown @command="switchLanguage" trigger="click">
      <span class="el-dropdown-link">
        {{ locale === 'zh' ? '中文' : 'English' }}
        <el-icon class="el-icon--right"><arrow-down /></el-icon>
      </span>
      <template #dropdown>
        <el-dropdown-menu>
          <el-dropdown-item command="zh" :disabled="locale === 'zh'">
            {{ t('message.chinese') }}
          </el-dropdown-item>
          <el-dropdown-item command="en" :disabled="locale === 'en'">
            {{ t('message.english') }}
          </el-dropdown-item>
        </el-dropdown-menu>
      </template>
    </el-dropdown>
  </div>
</template>
