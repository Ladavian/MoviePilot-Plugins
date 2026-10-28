# StrmFlow × MoviePilot V3

这是 StrmFlow 的 MoviePilot V3 配套插件，使用稳定 SDK、公开订阅 Oper 和公开 SubscribeChain。本插件支持 MoviePilot **V3**，版本 1.0.1。源码位于仓库根目录 `plugins.v3/strmflowbridge`，市场索引为根目录 `package.v3.json`。这是独立公开项目，StrmFlow 主程序继续保存在私有仓库中。仓库不包含任何用户的密钥、站点配置或媒体数据。

支持 StrmFlow v0.2.0 的联动协议 v1。插件版本与 StrmFlow 版本分别维护。

## 配置

1. 在 StrmFlow「上传与 STRM → 本地上传」建立自动上传任务，映射你的硬链接目录，启用外部通知。设置「外部来源目录」为 **MP 容器实际看到的整理后路径**，本地来源目录为 **StrmFlow 容器实际看到的对应路径**。
2. 在「发现与订阅 → 订阅设置 → MoviePilot 联动」启用，选择上述上传任务，保存后复制专用联动密钥。不使用后台管理员密码或 JWT。
3. 在 MP「插件市场设置」添加独立仓库 `https://github.com/Ladavian/strmflow-moviepilot`，刷新市场后安装 **StrmFlow 联动**。这是公开插件源，不需要授权读取 StrmFlow 私有主仓库。后续版本在 MP 插件市场中正常更新。多个插件源按 MP 设置要求用逗号分隔。

   如需手工安装，可以下载本项目 Release 的 `strmflow-moviepilot-v3.zip`。开发调试时也可使用 `PLUGIN_LOCAL_REPO_PATHS` 指向解压后的本目录。
4. 在 MP 插件中填写可访问的 StrmFlow 根地址和专用密钥，启用。填写根地址，例如 `http://strmflow:18766`，不要附加 `/api`。MP 不在同一个 Docker 网络时使用 NAS 的局域网地址及实际管理端口。
5. StrmFlow 的订阅转存位置沿用「发现与订阅」默认转存库；请先配置它和可用的资源来源。首次订阅镜像会立即排一次检查；后续检查使用 StrmFlow 的订阅计划。

## 工作方式

- 初次连接和每 15 秒读取 MP 订阅，匹配 TMDB 的电影、剧集及季/集段范围。MP 主动新增、修改和取消订阅会触发合并通知；不会覆盖原本手工建立的 StrmFlow 订阅范围。
- MP 只在 `TransferComplete` 且实际目标视频成功时，记录已入库事实并通知 StrmFlow 上传。下载源路径、失败整理、单纯的 `DownloadAdded` 都不能当作完成。
- 通知写入插件自有持久化队列；未确认的通知会在下一轮重试，重复通知幂等。单次处理至多 100 条通知。上传映射异常时保留路径通知重试，同时继续对账已入库事实；未绑定上传任务时可仅做订阅联动。无 TMDB 身份的成功硬链接文件仍可通知上传，不猜测其媒体身份。
- StrmFlow 读取真正有效的 STRM/115 台账，向 MP 返回自己已有的电影及季集。MP 下载选择前短时对账；完整电影/单集直接跳过，已知季集的剧集包用 `allowed_episodes` 限制缺少的集。
- MP 已入库事实单独存储，参与 StrmFlow 缺集计算，在转存前再检查，不伪装为本地 STRM。StrmFlow 获取的文件仍须进入本人 115、核验并正常整理生成 STRM 后才算已有。
- MP 的电影、整季/集段范围确实补齐后，通过公开 `SubscribeChain.finish_subscribe_or_not` 完成，让 MP 正常写入历史及发布完成事件。部分剧集只更新 `note`，不会结束整季。

## 边界与验证

- 自定义剧集组、非 TMDB 且没有辅助 TMDB 身份的条目、洗版订阅不自动镜像，避免跨编号体系错误去重。无法确定的季/集号不会猜测。
- 跨系统通知不能原子撤回已经开始的 PT 下载或网盘转存。完成通知到达后阻止后续重复获取；不删除正在运行的下载、做种任务或媒体文件。
- 没有文件名/元数据中明确集号、也没有已知季集清单的整季包，不做猜测性逐集限制。
- 当前已经通过纯协议、模拟 V3 宿主生命周期与完成语义，以及 Go 端认证、幂等、季集范围测试。尚未在真实 MP V3 宿主安装加载或真实 PT/115 账号上联调，不能将这些测试等同于线上验证。
- 官方参考：[V3 开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)、[V3 事件合同](https://github.com/jxxghp/MoviePilot/blob/v3/app/schemas/event.py)。
