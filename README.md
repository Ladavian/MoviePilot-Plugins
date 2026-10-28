# MoviePilot Plugins

面向 **MoviePilot V3** 的第三方插件库，提供媒体自动化、应用联动与效率扩展。各插件独立配置和维护，可按需安装。

## 安装

在 MoviePilot 的插件市场设置中添加：

```text
https://github.com/Ladavian/MoviePilot-Plugins
```

保存后刷新插件市场，搜索需要的插件并安装。配置多个插件源时，按 MP 设置要求使用逗号分隔。

本仓库为公开插件源。GitHub 连接及访问令牌设置沿用 MP 的常规要求；无需访问其他应用的私有源码仓库。

## 插件列表

| 插件 | 功能 | 使用说明 |
| --- | --- | --- |
| StrmFlow 联动 | 硬链接完成通知、订阅同步、电影与剧集逐集双向去重 | [安装与配置](plugins.v3/strmflowbridge/README.md) |

后续插件会继续加入本列表；插件的具体配置、兼容范围及验证情况见各自说明。

## 升级

在 MP 插件市场检查插件更新后直接升级。插件版本以市场索引 `package.v3.json` 为准，各插件分别维护自己的版本与更新记录。

仓库 Release 提供 `moviepilot-plugins-v3.zip`，包含插件索引、源码、图标与说明，可用于手工安装或本地调试。

## 项目结构

```text
plugins.v3/<插件ID小写>/   插件源码与说明
package.v3.json           MP V3 市场索引
icons/                    插件图标
tests/v3/<插件ID小写>/     协议及宿主合同测试
```

开发与维护遵循 [MoviePilot V3 插件开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)。提交前应核对版本、市场索引和更新记录，执行相应测试，并在真实 MP V3 宿主中验证插件。
