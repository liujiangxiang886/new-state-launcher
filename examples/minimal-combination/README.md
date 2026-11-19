# 最小组合（开发者校验模板）

配套文档：`docs/扩展打包规范-开发者校验.md` 第 2 节。

## 三个概念

| 名称 | 对应目录 / 文件 | 作用 |
|---|---|---|
| 最小单包 A | `minimal/` → `me.minimal-1.0.0.zip` | 只含 4 个必填字段 + 1 个文件，证明包结构能过准入 |
| 最小共存包 B | `companion/` → `me.minimal_companion-1.0.0.zip` | 演示 `requires` + `load_after`，证明组合解析能过准入 |
| 最小验证流程 | 下面三条命令 | 把自己的包和 A、B 一起跑，发之前最后一道保险 |

## 怎么用

1. 复制 `minimal/` 整个目录，改 `id`、`name`、`version`，把 `mods/me.minimal/`
   换成自己的命名空间。
2. 一条一条加改动（`files[]` → `data_patches[]` → `text_patches[]` →
   `method_hooks[]`），每加一条跑一次 `verify`，绿了再加下一条。
3. 发之前把 A、B 和自己的包一起跑组合验证。

```bash
python jjm_pack.py pack minimal/ -o me.my_ext-1.0.0.zip
python jjm_pack.py verify me.my_ext-1.0.0.zip
python jjm_pack.py verify me.my_ext-1.0.0.zip \
    --with me.minimal-1.0.0.zip --with me.minimal_companion-1.0.0.zip
python tools/verify_admission.py me.my_ext-1.0.0.zip \
    --with me.minimal-1.0.0.zip --with me.minimal_companion-1.0.0.zip --json
```

## 为什么 A 一定能过

它零补丁、零钩子、零依赖，且只写 `mods/me.minimal/` 这一个私有路径，
不会和任何包抢位置。开发者校验的四道关里，第 1、2 关只看清单形状，
第 3、4 关看组合重叠——它两边都干净，所以必然 `ADMITTED`。

## 为什么 B 也不会被拒

`requires` 指向 A、`load_after` 排在 A 之后，但 B 写的是
`mods/me.minimal_companion/`，与 A 的位置不重叠。依赖闭环、顺序可拓扑排序、
无未处置重叠，组合解析就不会产生 `error`。

> 注意：`load_after` **不消除冲突**。如果两个包改同一处，光写顺序仍然是重叠，
> 开发者校验照样拒装。要避让（改深指针）或接管（`overrides` + `requires` + `expect`）。
