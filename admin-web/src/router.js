import { createRouter, createWebHistory } from 'vue-router'
import Login from './views/Login.vue'
import Layout from './views/Layout.vue'
import Overview from './views/Overview.vue'
import Requests from './views/Requests.vue'
import Bundles from './views/Bundles.vue'
import BundleDetail from './views/BundleDetail.vue'
import Candidates from './views/Candidates.vue'
import Aliases from './views/Aliases.vue'
import Import from './views/Import.vue'
import ImportJobs from './views/ImportJobs.vue'
import Foods from './views/Foods.vue'
import FoodDetail from './views/FoodDetail.vue'
import { tokens } from './api'

const router = createRouter({
  history: createWebHistory('/console/'),
  routes: [
    { path: '/login', component: Login },
    {
      path: '/',
      component: Layout,
      children: [
        { path: '', redirect: '/overview' },
        { path: 'overview', component: Overview },
        { path: 'requests', component: Requests },
        { path: 'bundles', component: Bundles },
        { path: 'bundles/:id', component: BundleDetail },
        { path: 'candidates', component: Candidates },
        { path: 'aliases', component: Aliases },
        { path: 'import-new', component: Import },
        { path: 'imports', component: ImportJobs },
        { path: 'foods', component: Foods },
        { path: 'foods/:cid', component: FoodDetail },
      ],
    },
  ],
})

router.beforeEach((to) => {
  if (to.path !== '/login' && !tokens.access) return '/login'
})

export default router
