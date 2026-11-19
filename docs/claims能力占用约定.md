# claims 能力占用约定

> 面向扩展作者。1.7 起 4.6 / 4.7 双线通用。
> 一句话：`claims` 是作者主动声明「我完整替代了哪套游戏规则」；同一规则被两个包
> 声明 → **拒装，不可豁免**。

---

## 1. 为什么需要它

静态检查能看出「改了同一行」「写了同一个 JSON 字段」，但看不出：

| 包 A | 包 B | 静态结论 | 实际 |
|---|---|---|---|
| 改强化成功概率 | 改强化保底计数 | 代码不相交，可共存 | 两套规则叠加，数值荒谬 |

只有作者知道「我这套是完整重写强化规则，跟另一套不可能共存」。
`claims` 是表达这件事的唯一机制。

---

## 2. 命名

```
rule:<系统>.<规则>     完整替代某套数值/算法/进程规则
ui:<区域>.layout       完整替代某个界面布局
story:<序列>.flow      完整替代某段剧情流程（4.7，占位）
```

- 全小写，`.` 分层，`:` 分隔类别与主体；
- 不要用包 id 当键；
- 不要自创类别前缀。

---

## 3. 键表（当前约定）

### 通用规则

| 键 | 覆盖 | 依据脚本（4.7） |
|---|---|---|
| `rule:equipment.enhancement` | 强化概率、扣费、失败惩罚、保底 | `enhance_panel.gd` |
| `rule:equipment.enhancement_transfer` | 强化值转移资格、扣费、结果 | `transfer_panel.gd` |
| `rule:experience.progression` | 经验曲线、升级结算 | `flash/experience_curve.gd` |

### 装备链（4.7 细化）

| 键 | 覆盖 |
|---|---|
| `rule:equipment.inlay` | 镶嵌槽位、宝石效果 |
| `rule:equipment.manufacture` | 制造配方、成功率、材料 |
| `rule:equipment.compound` | 合成规则 |

### 战斗与掉落

| 键 | 覆盖 |
|---|---|
| `rule:combat.damage` | 伤害公式、暴击、减伤 |
| `rule:combat.random` | 战斗随机数来源 |
| `rule:loot.table` | 掉落表、概率 |

### 系统规则

| 键 | 覆盖 |
|---|---|
| `rule:dimension.rift` | 次元裂缝 |
| `rule:dungeon.progress` | 副本进度、通关判定 |
| `rule:pet.capture` | 宠物捕获概率 |
| `rule:pet.growth` | 宠物成长、属性 |
| `rule:machine.upgrade` | 机器升级 |
| `rule:workshop.recipe` | 工坊配方 |
| `rule:mission.track` | 任务追踪、完成判定 |
| `rule:activity.rules` | 活动规则 |
| `rule:skill.commands` | 技能指令解析 |
| `rule:shop.pricing` | 商店价格 |

### 界面布局

`ui:inventory.layout` / `ui:warehouse.layout` / `ui:role_info.layout` / `ui:hud.layout`

### 剧情（占位）

`story:dialogue.flow` / `story:opening.sequence`。**暂不强制**，先不要用。

---

## 4. 三条硬约定

### ① 新增内容不占 claims

| 你在做什么 | 声明 |
|---|---|
| 新增装备 / 技能 / 独立窗口 | ❌ 不占 |
| 改强化公式 | ✅ `rule:equipment.enhancement` |
| 改经验曲线 | ✅ `rule:experience.progression` |
| 整体替换背包布局 | ✅ `ui:inventory.layout` |

判断标准：**「我在加东西，还是改原有规则？」** 加东西不占，改规则才占。
给所有包都加 claims 会造成大量假冲突，比不写更糟。

### ② 冲突不可豁免

- 没有「保留前者 / 采用后者」；
- 不能用 `decisions.json` 绕过；
- 不能通过 `extensions.strict_exempt` 豁免。

### ③ 按线独立

`claims` 写在每条线各自的清单里。4.6 与 4.7 的占用互不影响；跨线不比较。

---

## 5. 写法

```jsonc
{
  "schema": 4,
  "claims": ["rule:equipment.enhancement"]
}
```

启动器在加载顺序解析阶段比较 claims；同键即冲突，拒装并列出双方 id。
