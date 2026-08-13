# Ariadne 可视化前端面板 — 详细设计

> 2026-08-12 | v3.0 — 纯 3D (3d-force-graph + Three.js)

---

## 一、设计目标

| 目标 | 说明 |
|------|------|
| **响应速度** | 图层切换 < 100ms，聚焦 < 200ms，首屏加载 < 3s |
| **大图可读** | 200+ 节点通过 Z 轴深度 + 图层过滤 + 聚焦模式协同，不拥挤 |
| **零服务端** | 纯静态 HTML，数据内嵌，`python -m src.viz` 一键生成 |
| **可干预** | 已对接 CRUD 控制面板 + MCP `dba_intervene` 工具，面板中可直接创建/编辑/删除节点和边 |
| **可持久** | CRUD 操作自动写回 YAML checkpoint，刷新网页或重启服务器后数据不丢失（详见 [DBA工程化落地计划](DBA工程化落地计划.md)） |

### 为什么纯 3D

2D 力导向在 50+ 节点时不可逆地塌缩为"毛线球"——节点沿 XY 平面扩散，连线交叉无法避免。图层 toggle 能过滤但不能解决"可见但看不清"的问题。

3D 的 Z 轴提供天然的第三维度分离——同屏 200 节点在 3D 空间中自然散开，旋转视角可观察原本被遮挡的连线。配合图层和聚焦，操作效率高于 2D + filter 组合。

---

## 二、技术选型

| 组件 | 选型 | 理由 |
|------|------|------|
| 图渲染 | **3d-force-graph** (Three.js) | WebGL 渲染、`nodeThreeObject` 自定义 3D 节点、`linkDirectionalParticles` 流向粒子、CDN 零安装 |
| 布局 | d3-force-3d | 三维力导向，Z 轴参与物理计算 |
| 标签 | CSS2DRenderer | HTML 标签叠加在 3D 场景上，清晰可读 |
| 相机 | Three.js OrbitControls | 旋转/平移/缩放，damping 惯性 |
| 样式 | 暗色场景 + 发光节点 | 专业 3D 仪表盘风格 |

### 技术对比

| 维度 | 2D (force-graph) | 3D (3d-force-graph) |
|------|:---:|:---:|
| 渲染 | Canvas 2D | **WebGL / Three.js** |
| 大图表现 | 50+ 节点开始拥挤 | 200+ 节点 Z 轴自然分离 |
| 重叠处理 | 纯靠 filter | filter + 旋转视角 + Z 轴深度 |
| 节点样式 | 2D 几何形状 | 球体 + 发光 + 粒子环 |
| 边可见性 | 交叉遮挡 | 旋转可露出被挡边 |
| 视觉冲击 | 一般 | 强 |
| CDN | ~80KB | ~500KB |
| GPU 要求 | 无 | 需要 WebGL（现代浏览器标配） |

---

## 三、面板布局

### 3.1 布局原则

3D 图**全屏居中**，左右两侧悬浮窗半透明叠加。悬浮窗不挤占 3D 场景空间，鼠标穿透空白区域直达 3D 交互。

```
+--------------------------------------------------------------+
| ┌────────────┐                        ┌────────────────────┐ |
| │  图层过滤   │                        │  选中节点详情        │ |
| │            │     3D 场景全屏          │                    │ |
| │ ☑ STATUS  │                        │ ID: n12            │ |
| │ ☑ REASON  │   ┌──────────────┐     │ 类型: ACTION       │ |
| │ ☑ ACTION  │   │              │     │ 内容: "靠喝咖啡..." │ |
| │ ☑ THING   │   │ 3d-force-    │     │ 邻居: 4            │ |
| │ ☑ PERSON  │   │ graph        │     │                    │ |
| │ ☑ EMOTION │   │ (Three.js)   │     │ [聚焦1-hop]        │ |
| │            │   │              │     │ [废弃节点]         │ |
| │ ───────── │   │              │     │                    │ |
| │ ☑ CAUSAL  │   │              │     └────────────────────┘ |
| │ ☑ PREF... │   │              │                             |
| │ ...       │   └──────────────┘                             |
| │            │                                               |
| │ [全图]    │                                               |
| └────────────┘                                               |
+--------------------------------------------------------------+
| ┌──────────────────────────────────────────────────────────┐ |
| │ 底部信息栏: 214N/266E | 选中: n12 | FPS: 60 | 聚焦: ON  │ |
| └──────────────────────────────────────────────────────────┘ |
```

### 3.2 悬浮窗样式

```
左侧悬浮窗（图层控制）           右侧悬浮窗（节点详情）
┌─────────────────────┐        ┌─────────────────────┐
│ 图层                  │        │ 选中节点              │
│                     │        │                     │
│ 节点类型              │        │ ID: n12            │
│ ☑ STATUS   ☑ REASON │        │ 类型: ACTION       │
│ ☑ ACTION   ☑ THING  │        │ 内容: "用户靠喝咖啡  │
│ ☑ PERSON   ☑ EMOTION│        │       提神保持清醒"  │
│                     │        │                     │
│ 边类型                │        │ 邻居: 4 (2入 2出)  │
│ ☑ CAUSAL  ☑ PREF... │        │                     │
│ ☑ SCENARIO☑ SEQUE...│        │ [聚焦 1-hop]        │
│ ☑ SOCIAL  ☑ ATTRI...│        │ [聚焦 2-hop]        │
│ ☑ TEMPORAL☑ TAXON...│        │ ───────────        │
│ 状态                  │        │ [废弃此节点]         │
│ ☑ 显示废弃  ☐ 遗忘   │        │ [删除所有连边]       │
│                     │        └─────────────────────┘
│ 渲染                  │
│ ☑ 流向粒子  ☑ 管道   │
│                     │
│ [隐藏全部边]         │
│ [仅高连接度] (deg≥中位数)│
│ [全图]               │
└─────────────────────┘

CSS 规范:
  position: fixed
  background: rgba(10, 14, 24, 0.85)    ← 半透明暗色底
  backdrop-filter: blur(12px)            ← 毛玻璃效果
  border: 1px solid rgba(255,255,255,0.08)
  border-radius: 10px
  padding: 16px
  font-size: 13px
  color: #c8d6e5
  pointer-events: auto                  ← 面板内可点击
  z-index: 100

  面板外空白区域: pointer-events: none  ← 鼠标穿透直达 3D 场景
```

### 3.3 三区位置

| 区域 | 位置 | 尺寸 | 折叠 |
|------|------|------|:---:|
| 左侧面板 | `left: 16px; top: 16px` | 宽 220px | 点击 ☰ 折叠为图标，hover 展开 |
| 3D 场景 | `position: absolute; inset: 0` | 全屏 | — |
| 右侧面板 | `right: 16px; top: 16px` | 宽 240px | 无选中节点时隐藏，选中后滑入 |
| 底部状态栏 | `bottom: 0; left: 0; right: 0` | 高 32px | 始终可见 |
| 搜索框 | 顶部居中悬浮 | 宽 360px | 始终可见 |

### 3.4 响应式行为

- 窗口宽度 < 900px：左右面板自动折叠为图标，hover 展开
- 窗口高度 < 600px：右侧面板缩小字号，隐藏内容预览只显示 ID+类型
- 移动端：仅 3D 场景 + 底部浮动操作按钮（只读模式）

---

## 四、图层系统设计

### 4.1 三层控制模型

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  原始全图     │────→│  图层过滤    │────→│  聚焦模式    │ = 屏幕可见
│  216节点     │     │  类型/边/     │     │  click节点   │
│  266边       │     │  废弃开关    │     │  1-hop展开   │
└─────────────┘     └─────────────┘     └─────────────┘
                         ↓                      ↓
                   graphData 重建          material.opacity
                   模拟重置 ~100ms         / visible 控制
                                            < 50ms
```

### 4.2 图层过滤（graphData 重建）

取消勾选节点类型 → 从 graphData 中移除对应节点和关联边 → 力导向重新收敛。200 节点重建成本 ~80ms。

**重建后相机行为**：`graph.graphData()` 会重置力导向，所有节点回到随机位置重新收敛。`applyFilters()` 在设置新数据后：
1. 立即 `graph.zoomToFit(200)`
2. 200ms 后（初步收敛）再次 `graph.zoomToFit(400)`
3. 如果当前处于聚焦模式，500ms 后重新执行 `applyFocusVisibility()`（因为 graphData 重建会重置所有 material）

```javascript
function applyFilters() {
    const visible = allNodes.filter(n => {
        if (!nodeTypeFilters[n.node_type]) return false;
        if (!showDeprecated && n.deprecated) return false;
        if (!showForgotten && n.forgotten) return false;
        return true;
    });
    const vIds = new Set(visible.map(n => n.id));
    const visibleLinks = allLinks.filter(l =>
        vIds.has(l.source.id || l.source) &&
        vIds.has(l.target.id || l.target) &&
        edgeTypeFilters[l.rel_type]
    );
    graph.graphData({ nodes: visible, links: visibleLinks });
    graph.d3ReheatSimulation();
    graph.zoomToFit(200);
    setTimeout(() => graph.zoomToFit(400), 200);
    if (focusNode) setTimeout(() => applyFocusVisibility(), 500);
}
```

### 4.3 聚焦模式（material 直接驱动，不重建 simulation）

点击节点 → 自动进入聚焦模式。非邻居节点和边通过直接操作 Three.js material 隐藏，保持物理模拟不变。**聚焦模式下图层 toggle 仍然生效**——可以在聚焦邻域内按类型进一步筛选。

> **技术注意**：因为使用了 `nodeThreeObject` 自定义几何体（八面体/十二面体），`nodeColor()` 对自定义 ThreeObject 无效。聚焦透明度必须直接操作 mesh 的 `material.opacity`。维护 `nodeId → material[]` 映射表。

```
进入聚焦:  (不重建 graphData)
  非邻居节点: material.transparent=true, material.opacity=0.05
  非邻居边:   linkVisibility = false
  1-hop 邻居: 恢复 opacity=1.0 + emissive 轻微提亮
  被聚焦节点: scale 1.5x + 金色发光 + 旋转粒子环
  相机:       flyTo 聚焦节点

退出聚焦: (ESC / 点击空白 / [返回])
  全部恢复 opacity = 1.0, transparent=false, linkVisibility=true
  相机 zoomToFit
```

```javascript
let focusNode = null;
let focusNeighbors = new Set();
const materialMap = new Map();  // nodeId → [material, ...] — 在 nodeThreeObject 中注册

function enterFocus(node) {
    focusNode = node;
    focusNeighbors = getNeighbors(node.id, 1);
    applyFocusVisibility();
    graph.cameraPosition(
        { x: node.x, y: node.y, z: node.z + 150 }, node, 2000
    );
}

function exitFocus() {
    focusNode = null;
    focusNeighbors.clear();
    applyFocusVisibility();
    graph.zoomToFit(400);
}

function applyFocusVisibility() {
    // 遍历所有节点，直接操作 material
    graph.graphData().nodes.forEach(n => {
        const mats = materialMap.get(n.id) || [];
        if (!focusNode) {
            // 退出：全部恢复
            mats.forEach(m => { m.transparent = false; m.opacity = 1; });
        } else if (n.id === focusNode.id) {
            mats.forEach(m => { m.transparent = false; m.opacity = 1; });
            n.__isFocused = true;
        } else if (focusNeighbors.has(n.id)) {
            mats.forEach(m => { m.transparent = false; m.opacity = 1; });
            n.__isFocused = false;
        } else {
            mats.forEach(m => { m.transparent = true; m.opacity = 0.05; });
            n.__isFocused = false;
        }
    });
    // 边可见性
    graph.linkVisibility(l => {
        if (!focusNode) return true;
        return focusNeighbors.has(l.source.id) && focusNeighbors.has(l.target.id);
    });
}
```

在 `nodeThreeObject` 回调中注册 material：

```javascript
nodeThreeObject(node) {
    const group = new THREE.Group();
    // ... 创建 mesh ...
    const mesh = new THREE.Mesh(geometry, material);
    group.add(mesh);
    
    // 注册
    if (!materialMap.has(node.id)) materialMap.set(node.id, []);
    materialMap.get(node.id).push(material);     // 主 mesh
    if (node.deprecated) materialMap.get(node.id).push(wireframeMat);
    if (node.__isFocused) materialMap.get(node.id).push(ringMat);
    
    return group;
}
```

### 4.4 聚焦模式下的图层交互（核心）

聚焦模式和图层 toggle 是两个正交维度，可以叠加使用：

```
                    ┌──────────────────────┐
                    │   图层 toggle         │
                    │   (graphData 重建)    │
                    │   控制哪些类型存在     │
                    └──────────┬───────────┘
                               │
                               ▼
                    ┌──────────────────────┐
                    │   聚焦模式            │
                    │   (material opacity) │
                    │   控制已存在节点可见性  │
                    └──────────────────────┘
```

| 场景 | 操作 | 行为 |
|------|------|------|
| 全图 + toggle STATUS 关 | 取消勾选 STATUS | 所有 STATUS 节点+关联边从 graphData 移除，力导向重新布局 |
| 聚焦 n1 + toggle PERSON 关 | 在聚焦 n1 时取消 PERSON | PERSON 节点从 graphData 移除 → 力导向重布局 → 聚焦仍在 n1 → 可见范围自动更新 |
| 聚焦 n1 + 双击邻居 | 双击 n1 的邻居 n3 | 聚焦切换为 n3，2-hop 展开（`getNeighbors(n3, 2)`） |
| 聚焦 n1 + toggle ACTION 关再开 | 关→开 ACTION | 关时 ACTION 移除 → 重布局。开时 ACTION 重新加入 graphData，恢复可见 |
| 聚焦 n1 + 搜索 | 在聚焦状态下搜索 | opacity 叠加：非匹配且非邻居 → 0.02；非匹配但是邻居 → 0.2；匹配 → 1.0 |

实现要点：`applyFilters()` 始终操作 `allNodes`/`allLinks` 的完整快照，过滤后写入 graphData。聚焦模式独立跟踪 `focusNode` + `focusNeighbors`，在 `applyFilters()` 之后调用 `applyFocusVisibility()` 叠加效果。两者互不感知对方状态。

```javascript
// 图层切换回调（无论是否聚焦中）
function onNodeTypeToggle(type, checked) {
    nodeTypeFilters[type] = checked;
    applyFilters();              // graphData 重建
    applyFocusVisibility();      // 如果聚焦中，重新应用透明度
}

// 搜索回调（无论是否聚焦中）
function onSearch(query) {
    if (!query) {
        graph.nodeColor(n => n.__color);       // 恢复
        if (focusNode) applyFocusVisibility();  // 重新应用聚焦
        return;
    }
    const matched = allNodes.filter(n => n.content.includes(query));
    const matchIds = new Set(matched.map(n => n.id));
    graph.nodeColor(n => {
        if (matchIds.has(n.id)) return '#00FFFF';  // 青色高亮
        if (focusNode && focusNeighbors.has(n.id)) return 'rgba(255,255,255,0.2)';
        return 'rgba(255,255,255,0.03)';
    });
}
```

### 4.5 右键菜单：按类型隐藏

右键节点或空白 → 上下文菜单，提供快捷隐藏操作：

```
右键节点 → 菜单:
  ┌──────────────────────┐
  │ 聚焦此节点 (1-hop)    │
  │ 聚焦此节点 (2-hop)    │
  ├──────────────────────┤
  │ 隐藏本节点             │
  │ 隐藏 [STATUS] 类型   │  ← 动态根据被右键节点的类型显示
  │ 隐藏无关联节点         │
  ├──────────────────────┤
  │ 废弃此节点             │
  │ 复制节点 ID           │
  └──────────────────────┘

右键空白 → 菜单:
  ┌──────────────────────┐
  │ 显示全部              │
  │ 隐藏全部边            │
  │ 隐藏废弃节点           │
  │ 仅显示 CAUSAL 链路    │  ← 隐藏非 CAUSAL 边 + 无 CAUSAL 边的节点
  └──────────────────────┘
```

---

### 4.6 搜索（material 驱动）

搜索不重建 graphData——匹配节点高亮发光，非匹配降低透明度。聚焦状态下搜索叠加聚焦透明度规则。相机自动 fly 到匹配区域。

---

## 五、3D 视觉设计

### 5.1 节点样式

| 节点类型 | 颜色 | 3D 表现 | 半径 |
|---------|------|---------|:---:|
| STATUS | #5DADE2 蓝 | 球体 + 淡蓝发光 | 3 |
| REASON | #E74C3C 红 | 球体 + 淡红发光 | 3 |
| ACTION | #F39C12 橙 | 球体 + 淡橙发光 | 3 |
| THING | #27AE60 绿 | 正八面体 + 淡绿发光 | 2.5 |
| PERSON | #A569BD 紫 | 正十二面体 + 淡紫发光 | 2.5 |
| EMOTION | #F4D03F 黄 | 球体 + 淡黄发光 | 2.5 |

```javascript
// 节点 Three.js 对象
nodeThreeObject(node) {
    const group = new THREE.Group();
    let geometry;
    
    if (node.node_type === 'THING')
        geometry = new THREE.OctahedronGeometry(node.radius * 0.7);
    else if (node.node_type === 'PERSON')
        geometry = new THREE.DodecahedronGeometry(node.radius * 0.7);
    else
        geometry = new THREE.SphereGeometry(node.radius * 0.7, 16, 16);
    
    const material = new THREE.MeshStandardMaterial({
        color: NODE_COLORS[node.node_type],
        roughness: 0.4,
        metalness: 0.2,
        emissive: NODE_COLORS[node.node_type],
        emissiveIntensity: 0.3,
    });
    
    group.add(new THREE.Mesh(geometry, material));
    
    // 废弃节点：线框覆盖
    if (node.deprecated) {
        const wireframe = new THREE.Mesh(geometry, 
            new THREE.MeshBasicMaterial({ color: 0x888888, wireframe: true }));
        group.add(wireframe);
    }
    
    // 聚焦节点：发光环
    if (node.isFocused) {
        const ring = new THREE.Mesh(
            new THREE.TorusGeometry(node.radius, 0.3, 8, 24),
            new THREE.MeshBasicMaterial({ color: 0xFFD700 })
        );
        group.add(ring);
    }
    
    return group;
}
```

### 5.2 边样式

3D 下边用**管道 + 流向粒子**表示，比 2D 线条更直观：

| 边类型 | 颜色 | 粒子速度 | 管道宽度 |
|--------|------|:---:|:---:|
| CAUSAL | #E74C3C 红 | 快 | 0.8 |
| SCENARIO | #27AE60 绿 | 慢 | 0.6 |
| SEQUENCE | #3498DB 蓝 | 中 | 0.6 |
| PREFERENCE | #F39C12 橙 | 中 | 0.6 |
| SOCIAL | #A569BD 紫 | 慢 | 0.5 |
| ATTRIBUTE | #1ABC9C 青 | 慢 | 0.4 |
| TEMPORAL | #95A5A6 灰 | 中 | 0.4 |
| TAXONOMIC | #7F8C8D 深灰 | 中 | 0.4 |

```javascript
// 管道 + 流向粒子
linkThreeObject(link) {
    // 用自定义几何体画管道（或直接用 3d-force-graph 内置的 linkDirectionalParticles）
}

// 启用内置粒子流（可由左侧面板开关控制）
graph.linkDirectionalParticles(2);          // 每条边 2 个粒子
graph.linkDirectionalParticleSpeed(link => {
    const speeds = { CAUSAL: 0.01, SEQUENCE: 0.006, PREFERENCE: 0.005, 
                     SCENARIO: 0.003, SOCIAL: 0.003, ATTRIBUTE: 0.002,
                     TEMPORAL: 0.005, TAXONOMIC: 0.005 };
    return speeds[link.rel_type] || 0.004;
});
graph.linkDirectionalParticleColor(link => EDGE_COLORS[link.rel_type]);

// 粒子/管道开关（左侧面板底部）
// ☑ 流向粒子  ☑ 管道渲染
// 低性能设备可关闭以保持 60fps
```

### 5.3 节点标签 (CSS2DRenderer)

3D 场景中标签需用 CSS2DRenderer 叠加 HTML：

- 默认：仅 hover 时显示标签
- 聚焦节点：标签始终显示 + 稍大字号
- 缩放时自动调整可见性：距离相机 > 500 单位 → 隐藏标签
- 标签样式：暗色半透明底 + 白色文字，11px

### 5.4 场景氛围

- 背景：纯黑 `#000011`
- 环境光：微弱蓝色（`0x112244`，intensity 0.4）
- 点光源：跟随相机移动
- 可选：星空粒子背景（1000 个小点随机分布）

---

## 六、交互清单

| 操作 | 触发 | 效果 |
|------|------|------|
| 旋转 | 鼠标左键拖 | OrbitControls，damping 惯性 |
| 平移 | 鼠标右键拖 / Shift+左键 | 平移场景（`canvas.oncontextmenu = e => e.preventDefault()` 阻止浏览器菜单） |
| 缩放 | 滚轮 | 0.1x ~ 20x（OrbitControls） |
| 点击节点 | 单击 | 选中 + 右侧面板 + 聚焦模式 + 相机 flyTo |
| 双击节点 | 双击 | 展开 1-hop 邻居聚焦 |
| 悬停节点 | hover | 高亮 + 标签显示 |
| 点击空白 | 单击 | 退出聚焦 + 相机归位 |
| 全图 | [全图] 按钮 | 退出聚焦 + `zoomToFit(400)` |
| 图层切换 | checkbox | graphData 重建 |
| 搜索 | 输入 | material 驱动透明度，相机 flyTo 匹配区域 |
| ESC | 键盘 | 退出聚焦 / 清除搜索 / 相机归位 |
| R | 键盘 | 重置相机俯视视角 |

### 相机预设快捷键

| 键 | 视角 | 适合 |
|:---:|------|------|
| 1 | 俯视 (Top) | 看全局聚类 |
| 2 | 正面 (Front) | 看因果链左右流向 |
| 3 | 侧面 (Side) | 看 Z 轴深度分离 |

---

## 七、数据模型

### 7.1 Python 导出格式

```python
{
    "nodes": [
        {
            "id": "n1",
            "label": "用户最近压力很大",
            "node_type": "STATUS",
            "content": "用户最近因项目快上线而压力很大，天天加班到十点",
            "deprecated": False,
            "forgotten": False,
            "in_degree": 3,
            "out_degree": 2,
            "radius": 3
        },
        ...
    ],
    "links": [
        {
            "source": "n2",
            "target": "n1",
            "rel_type": "CAUSAL",
            "bidirectional": False,
            "deprecated_src": False,
            "deprecated_dst": False
        },
        ...
    ],
    "stats": { "total_nodes": 216, "total_edges": 266, "deprecated": 5, "forgotten": 2, "orphans": 8 }
}
```

### 7.2 HTML 生成流程

```
MemoryGraph.to_3dforcegraph()
    ↓
JSON → 嵌入 HTML <script>
    ↓
3d-force-graph(graphData) 初始化
    ↓
d3-force-3d simulation → 200 ticks → 收敛
    ↓
material.opacity / graphData 重建 ← 图层控制
```

---

## 八、文件结构

```
src/viz/
├── __init__.py
├── exporter.py            # MemoryGraph.to_3dforcegraph()
├── renderer.py            # 读取 checkpoint → 生成自包含 HTML
└── templates/
    └── dashboard_3d.html  # 单文件前端（3d-force-graph CDN + Three.js + 全部交互）

使用:
  # 从检查点生成静态 HTML
  python -m src.viz.renderer --checkpoint snapshots/dba_20260812/

  # 测试阶段：手动触发重新生成
  # 后续按需添加 --watch 模式（定期自动刷新 JSON 数据）
```

---

## 九、性能预算

| 场景 | 操作 | 目标延迟 | 实现方式 |
|------|------|:---:|------|
| 首屏渲染 | 打开 HTML | < 3s | CDN ~500KB + 数据内嵌 |
| 物理收敛 | 首次 200 ticks | ~2-3s | d3-force-3d，收敛后关闭物理 |
| 图层切换 | toggle 类型 | < 100ms | graphData 重建 + 30 ticks 预热 |
| 聚焦 | 点击节点 | < 100ms | material.opacity + camera flyTo |
| 搜索 | 实时匹配 | < 50ms | material 驱动 |
| 旋转/缩放 | 鼠标/滚轮 | 60fps | WebGL 原生 |
| GPU 内存 | 200 节点场景 | ~15MB | 复用 geometry，不创建重复网格 |

### 性能关键决策

- **图层切换用 graphData 重建**（重建力导向 ~100ms，WebGL 几何体重建 ~50ms）
- **聚焦和搜索用 material 驱动**（不重建 graphData，不重置模拟，瞬间切换）
- **收敛后关闭 d3-force**（场景静止时零 CPU 模拟开销，GPU 只负责渲染）
- **geometry 复用**（同类型节点共享 geometry 实例，200 节点仅 4 个 geometry 对象）

---

## 十、不变更项

- 不引入 React/Vue —— 纯 DOM + 3d-force-graph
- 不引入后端服务 —— 纯静态 HTML
- 不与 MCP 耦合 —— 独立生成和使用
- 不提供 2D 模式 —— 3D 已经通过 Z 轴解决 2D 的拥挤问题

---

## 十一、实现总结

> 2026-08-12 | 3D 图渲染模块基本完成

### 11.1 已实现功能

| 功能 | 状态 | 说明 |
|------|:---:|------|
| 3D 力导向图渲染 | 完成 | `3d-force-graph@1.71.3` + `three@0.148.0`，200 ticks 收敛 |
| 图层过滤 | 完成 | 节点类型/边类型/废弃/遗忘 独立 toggle，graphData 重建 |
| 聚焦模式 | 完成 | 点击节点 → `nodeVisibility` 隐藏无关节点和边，聚焦节点金色高亮 |
| 节点拖拽固定 | 完成 | 拖拽后 `fx/fy/fz` 固定，3D 球面卫星环 + 半透明轨道表示固定状态 |
| 搜索下拉框 | 完成 | 模糊匹配 → 青色高亮 + 下拉列表，点击结果直接聚焦 |
| 右键菜单 | 完成 | 聚焦/hop/隐藏节点/隐藏类型/复制ID 等快捷操作 |
| 悬浮窗面板 | 完成 | 左侧图层控制 + 右侧节点详情，固定/释放按钮，类型标签着色 |
| 3D 节点样式 | 完成 | STATUS/REASON/ACTION/EMOTION 球体，THING 八面体，PERSON 十二面体，类型色发光 |
| 边流向粒子 | 完成 | 8 种边类型独立颜色 + 粒子速度，可开关 |
| 相机控制 | 完成 | OrbitControls 旋转/平移/缩放，快捷键 1/2/3 预设视角，R 归位 |
| 底部状态栏 | 完成 | 节点/边/废弃/遗忘/孤立/FPS 统计 |
| 响应式 | 完成 | 左侧面板 hover 展开，右侧面板选中显示 |

### 11.2 文件结构

```
src/viz/
├── __init__.py
├── exporter.py              # MemoryGraph → 3d-force-graph JSON
├── renderer.py              # CLI: python -m src.viz.renderer --yaml ... --output ...
└── templates/
    └── dashboard_3d.html    # 单文件前端（~600 行 JS）
```

### 11.3 关键设计决策

**CDN 版本锁定**
- `three@0.148.0` + `3d-force-graph@1.71.3` UMD 构建，避免 ESM importmap 子依赖问题
- UMD 版 3d-force-graph 内联 Three.js，但需手动引入同版本 global THREE

**source/target 变异**
- `graph.graphData()` 会将 link 的 source/target 从字符串变异为节点对象
- 所有过滤/邻居查找统一用 `l.source.id || l.source` 模式兼容

**materialMap 生命周期**
- `materialMap` 在每次 `graphData()` 前 clear，在 `nodeThreeObject` 中重建
- 聚焦卫星轨道 mesh 和半透明环 mesh 也注册到 materialMap
- 所有 material 操作需兼容 `MeshBasicMaterial`（无 `emissive`）和 `MeshStandardMaterial`

**nodeVisibility vs material opacity**
- 聚焦模式使用 `graph.nodeVisibility()` API 彻底隐藏节点，而非材质透明度
- 搜索高亮使用材质 `emissive` 着色 + `opacity` 区分

**拖拽固定**
- `onNodeDragEnd` 设置 `fx/fy/fz` 固定节点
- 固定节点显示 4 个 3D 球面轨道卫星（绕 X 轴倾斜 45°）+ 半透明环
- 轨道动画使用独立 `requestAnimationFrame` 循环，不依赖力模拟 tick

**模拟冷却策略**
- 初始加载 200 ticks 后 `charge` 降为 -30，仅做一次 `zoomToFit`
- 图层切换重置 `freezeTick=200` 让模拟重新稳定
- 不再自动 `zoomToFit`（用户反馈镜头拉远影响观感）

### 11.4 节点颜色表

| 类型 | 颜色 | 色值 | 几何体 |
|------|------|------|--------|
| STATUS | 蓝 | `#5DADE2` | 球体 |
| REASON | 红 | `#E74C3C` | 球体 |
| ACTION | 橙 | `#F39C12` | 球体 |
| THING | 绿 | `#27AE60` | 正八面体 |
| PERSON | 紫 | `#A569BD` | 正十二面体 |
| EMOTION | 玫红 | `#E91E63` | 球体 |

### 11.5 使用方式

```bash
# 从 YAML checkpoint 生成静态 HTML
python -m src.viz.renderer --yaml data/natural_person/memory_graph.yaml --output snapshots/natural_person_3d.html

# 浏览器直接打开生成的 HTML 即可使用（无需服务器）
```

---

## 十二、DBA CRUD 控制面板设计

> 2026-08-12 | v1.0

### 12.1 设计目标

在 3D 渲染模块之上叠加人工 CURD 操作能力，支持对 MemoryGraph 进行节点/边的增删改查。已封装为 MCP `dba_intervene` 工具（详见 [DBA工程化落地计划](DBA工程化落地计划.md)）。

核心原则：
- **渲染模块纯粹**：不增加编辑功能，只负责展示 + 暴露聚焦事件
- **CRUD 面板独立**：单独浮动窗，通过事件总线桥接
- **数据一致**：CRUD 操作直接修改图数据源，渲染模块增量更新
- **可撤销**：操作栈支持 Undo

### 12.2 架构

```
浏览器 (单页)
┌─────────────────────────────────────────────────────────┐
│  ┌────────────────┐         ┌────────────────────────┐  │
│  │  3D 渲染模块    │         │  CRUD 控制面板 (浮动)    │  │
│  │                │ 事件总线 │                        │  │
│  │ 点击节点 ──────┼──node──→│ 切换到该节点编辑模式      │  │
│  │                │ selected│                        │  │
│  │ (不做响应)      │←─ x ───│ CRUD 列表点击不联动 3D    │  │
│  │                │         │                        │  │
│  │ applyFilters() │←refresh│ 增删改后触发图刷新        │  │
│  └────────────────┘         └───────────┬────────────┘  │
│                                         │ HTTP           │
└─────────────────────────────────────────┼────────────────┘
                                          │
                               ┌──────────▼──────────┐
                               │  Python 后端 (Mock)   │
                               │  /api/nodes  CRUD    │
                               │  /api/edges  CRUD    │
                               │  /api/graph  导出     │
                               └─────────────────────┘
```

### 12.3 聚焦映射（单向）

**3D → CRUD**（实现）：
- 点击 3D 节点 → CRUD 面板自动切换到该节点的编辑模式，显示详情和关联边
- 点击 3D 空白 → CRUD 面板返回列表视图

**CRUD → 3D**（不做）：
- CRUD 列表点击节点 → 3D 不跟随。避免双向联动造成的循环更新和性能问题

### 12.4 CRUD 面板布局

```
┌──────────────────────────────────┐
│ DBA 控制台                    [_][×]    ← 可拖动标题栏
├──────────────────────────────────┤
│ [新建节点]  [新建边]  [撤销]  [导出]  │  ← 工具栏
│ 搜索节点: [__________________]      │
├──────────────────────────────────┤
│ ┌─ 节点列表 ──────── 共 42 个 ──┐ │
│ │ ● STATUS  压力很大              │ │  ← 点击切换到编辑模式
│ │ ● REASON  项目快上线            │ │
│ │ ● ACTION  天天加班到十点         │ │
│ │ ... (虚拟滚动，50条/页)         │ │
│ └──────────────────────────────┘ │
├──────────────────────────────────┤
│ ┌─ 节点编辑 ────────────────────┐ │
│ │ ID: n12                       │ │
│ │ 类型: [STATUS       ▼]        │ │
│ │ 内容:                         │ │
│ │ ┌──────────────────────────┐  │ │
│ │ │ 用户最近压力很大...        │  │ │
│ │ └──────────────────────────┘  │ │
│ │                               │ │
│ │ ┌─ 关联边 (入2 出3) ─────────┐│ │
│ │ │ ← CAUSAL   咖啡提神 (n42)  ││ │
│ │ │ ← SCENARIO 办公室 (n8)    ││ │
│ │ │ → SEQUENCE 辞职 (n55)     ││ │
│ │ │ → CAUSAL   效率下降 (n31)  ││ │
│ │ │ [+ 添加关联]               ││ │
│ │ └───────────────────────────┘│ │
│ │                               │ │
│ │ [保存]  [废弃]  [删除节点]     │ │
│ └──────────────────────────────┘ │
├──────────────────────────────────┤
│ 已修改 3 处    最后操作: 更新 n12  │  ← 状态栏
└──────────────────────────────────┘
```

### 12.5 增量更新策略

修改 `allNodes` / `allLinks` 内存数组后调用 `applyFilters()` 重建图。不重新从后端加载全量数据。

| 操作 | 内存变更 | 图更新 |
|------|---------|--------|
| 新建节点 | `allNodes.push(newNode)` | `applyFilters()` |
| 更新节点 | `allNodes[i] = updated` | `applyFilters()` |
| 删除节点 | `allNodes.splice(i,1)` + 级联删边 | `applyFilters()` |
| 新建边 | `allLinks.push(newLink)` | `applyFilters()` |
| 删除边 | `allLinks.splice(i,1)` | `applyFilters()` |

注意：`applyFilters()` 重建 graphData 会丢失以下状态，需恢复：
- 拖拽固定节点的 `fx/fy/fz`（已在 `allNodes` 上持久化，重建后自动生效）
- 聚焦状态（需在 `applyFilters()` 后重新调用 `applyFocusVisibility()`）

### 12.6 撤销 (Undo)

操作栈设计：

```javascript
const undoStack = [];   // { action, undoFn, label }
const MAX_UNDO = 50;

function pushUndo(action) {
  undoStack.push(action);
  if (undoStack.length > MAX_UNDO) undoStack.shift();
}

function undo() {
  const action = undoStack.pop();
  if (!action) return;
  action.undoFn();           // 执行反向操作
  applyFilters();            // 刷新 3D 图
  refreshCrudPanel();        // 刷新 CRUD 面板
}
```

每个 CRUD 操作记录包含：
- `label`: 显示文字（如"更新 n12"）
- `undoFn`: 执行反向操作的函数（调用后端 API + 更新内存数组）

撤销操作本身不产生新的撤销记录。

### 12.7 后端 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/graph` | 获取完整图数据 |
| `POST` | `/api/nodes` | 创建节点 `{node_type, content}` → 返回 `{id, ...}` |
| `PUT` | `/api/nodes/{id}` | 更新节点 `{content?, node_type?, deprecated?}` |
| `DELETE` | `/api/nodes/{id}` | 删除节点（级联删除关联边） |
| `POST` | `/api/edges` | 创建边 `{source, target, rel_type}` |
| `DELETE` | `/api/edges/{source}/{target}` | 删除两个节点之间的边 |
| `GET` | `/api/export/yaml` | 导出当前 YAML checkpoint |

> 后端已实现真实持久化：每次 CRUD 操作自动写回 YAML checkpoint，服务器重启后数据不丢失。详见 [DBA工程化落地计划](DBA工程化落地计划.md)。

### 12.8 边界处理

**删除聚焦节点**
```
选中节点 → [删除] → 确认对话框
→ 如果 selectedNode === focusNode → exitFocus()
→ DELETE /api/nodes/{id}
→ allNodes 中移除 → applyFilters()
```

**CRUD 面板选中被图层隐藏的节点**
```
CRUD 列表中有节点 n42，但用户在 3D 中 toggle 隐藏了它的类型
→ CRUD 面板仍可编辑 n42
→ [保存] 触发 applyFilters()，n42 因类型被隐藏不会显示
→ 用户体验：编辑了但看不到。接受此行为（图过滤不联动 CRUD 面板）
```

**双向边操作**
```
SCENARIO / SOCIAL / ATTRIBUTE 双向边：
→ 创建时只存一条，exporter 自动去重
→ 删除时 DELETE /api/edges/src/tgt 即可（双向解绑在后端处理）
→ CRUD 面板显示边时不标注"双向"，用户无需感知
```

### 12.9 样式规范

- 可拖动浮动窗，默认右下角 `right: 370px; bottom: 50px`，尺寸 `380 × 520`
- 暗色半透明底 `rgba(10,14,24,0.92)` + `backdrop-filter: blur(16px)`
- 与渲染面板风格统一：圆角 10px，边框 `rgba(255,255,255,0.08)`
- 标题栏 `height: 36px`，可拖拽移动
- 节点列表每行带类型色点 + 内容预览，hover 高亮
- 表单输入框暗底 `rgba(255,255,255,0.06)`，聚焦边框 `#5DADE2`
- 危险操作按钮（删除）红色

### 12.10 实现总结

> 2026-08-12 | CRUD 面板已集成到 dashboard_3d.html

#### 12.10.1 已实现功能

| 功能 | 状态 | 说明 |
|------|:---:|------|
| CRUD 浮动面板 | 完成 | 右下角可拖拽/折叠窗，含标题栏 + 工具栏 + 节点列表 + 节点编辑区 + 状态栏 |
| 节点列表 | 完成 | 搜索过滤 + 类型色点 + 分页（50条/页），点击切换到编辑模式 |
| 新建节点 | 完成 | 弹窗选类型(6种) + 输入内容 → 创建 → 3D 图增量添加 + 自动聚焦 |
| 编辑节点 | 完成 | 修改类型下拉/内容文本框 → [保存] → 3D 图立即刷新 |
| 废弃节点 | 完成 | 切换废弃状态 + 记忆原始 emissive 色值，恢复时复原 |
| 删除节点 | 完成 | 确认对话框(显示关联边数) → 级联删边 → 自动 exitFocus + 刷新全图 |
| 新建边 | 完成 | 弹窗：搜索源节点 → 搜索目标节点 → 选边类型 → 创建 → 3D 图增量添加 |
| 边列表 + 删除 | 完成 | 编辑模式下显示入/出边列表，带类型色和操作按钮，逐条删除 |
| 撤销 | 完成 | 操作栈最多 50 条，含 label + undoFn(调 API + 回写内存数组) |
| 导出 | 完成 | GET /api/export/yaml 下载 |
| 3D → CRUD 映射 | 完成 | 点击 3D 节点自动切换编辑模式，点击空白回列表视图 |
| 右侧面板连边/删边 | 完成 | 连接边(选类型→点目标) / 删除边(点已连接目标)，手势交互 + 错误提示 |
| 状态栏 | 完成 | 显示节点/边统计 + 已修改次数 + 最后操作描述 |

#### 12.10.2 文件结构

```
src/viz/
├── __init__.py
├── api_server.py            # Mock REST API (8 端点, 内存操作 MemoryGraph)
├── exporter.py              # MemoryGraph → 3d-force-graph JSON
├── renderer.py              # CLI 生成器
└── templates/
    └── dashboard_3d.html    # 单文件: 3D 渲染 + CRUD 面板 + API 桥接
```

#### 12.10.3 关键设计决策

**单向聚焦映射**
- 3D 点击节点 → CRUD 切到编辑模式（实现）
- CRUD 列表点击 → 3D 不跟随（避免循环更新 + 图过滤不一致）

**增量更新 + applyFilters 重建**
- 修改内存数组后统一调 `applyFilters()`，不重新加载全量数据
- 重建后恢复聚焦状态（`applyFocusVisibility()`）+ 固定节点位置（`fx/fy/fz` 持久化在 allNodes 上）

**撤销快照策略**
- 删节点：保存完整节点元数据 + 关联边列表，undo 时回插原数据
- 删边：保存边对象引用，undo 时回插
- 撤销本身不产生新撤销记录

**边方向处理**
- 后端 `has_edge` 是单向（DiGraph），前端 `hasEdgeBetween` 也改为单向
- 删除边时取实际边对象的方向调 API：`DELETE /api/edges/{actualSrc}/{actualTgt}`
- 连接边模式用单向检查（防重复），删除边模式用手势选择（不限制方向）

#### 12.10.4 代码审查修复记录

| 问题 | 修复 |
|------|------|
| 边方向不匹配（前端双向、后端单向） | `hasEdgeBetween` 改单向，取实际边方向调 API |
| `deleteCrudNode` 先删本地再调 API | 调换顺序：API 成功后再改本地 |
| 撤销节点删除时 ID 错乱 | 保存完整节点数据快照用于 undo |
| `renderCrudEditor` 参数可能 undefined | 所有调用处加 null guard |
| `FULL_DATA.stats` CRUD 后不更新 | 改为从 `allNodes` 实时计算 |
| 后端无自环检查 + 空字段 | 加校验 + 友好 400 错误 |
| `event` 全局变量不可靠 | 改为 `window.event` |
| 关闭按钮关闭后无法再打开 | 移除关闭按钮，只保留折叠/展开 |

#### 12.10.5 使用方式

```bash
# 1. 启动 API 服务器
python -m src.viz.api_server --yaml data/natural_person/memory_graph.yaml --port 8765

# 2. 生成 HTML
python -m src.viz.renderer --yaml data/natural_person/memory_graph.yaml --output snapshots/natural_person_3d.html

# 3. 浏览器打开 HTML，CRUD 面板自动连接 localhost:8765
```

> 如果 API 服务器未启动，CRUD 操作会报错提示。3D 渲染和图层操作无需后台。
