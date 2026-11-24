// 浮汐新增（不属于上游）：`node -r ./fuxi/preload.js server.js` 预加载，给内核路由表追加浮汐自己的接口，
// 不改上游文件。server.js 启动时解构 require('./src/http/routes').createRouter，这里先把它包一层。
'use strict';
const routes = require('../src/http/routes');

const original = routes.createRouter;
routes.createRouter = function createRouter(runtime) {
  const router = original(runtime);
  require('./asr')(router);
  return router;
};
