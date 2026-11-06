# 命名空间与来源混合说明

本文件使用中英文混合表述，仅作为检索质量固定集的合成文本。

## 来源仓库与依赖

文档修订通过 source_repository_id 记录内容实际来源的仓库。
一个来自分支仓库的修订还必须记录 dependency_repository_ids，其中包含基础仓库和分支仓库两个标识。
如果用户没有显式选择分支仓库，那么依赖集合不是选入范围的子集，该修订必须被拒绝。

## Namespace isolation

The vector filter is scoped by repository_id and index_version_id.
It is not a namespace-wide search: an unselected repository never contributes vectors.
混合查询必须同时携带 selected_repository_ids 和 indexes，两者不能互相推断。

## 来源与索引分离

A source-only repository contributes explicitly selected source documents without owning an index.
仅来源仓库不建立索引，也不应伪造一个空索引来满足接口。
覆盖范围 coverage 必须按每个冻结索引的真实代次报告，不能用一个仓库的代次代替另一个。

## 旧版本

旧 active SHA 只有在用户明确选择“使用旧 active SHA”时才能参与检索。
目标 SHA 与 active SHA 不同时，界面必须同时显示两者，不能把旧结果当作目标版本结果。
