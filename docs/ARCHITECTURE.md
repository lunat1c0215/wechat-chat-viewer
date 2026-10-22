# 架构说明

## 模块职责

| 模块 | 职责 |
|---|---|
| `src/微信聊天记录查看器.py` | 主窗口、会话列表、消息渲染、导出与统计；启动流程与后台线程调度 |
| `src/wxdecrypt.py` | 微信进程/版本/数据目录侦测、密钥提取、SQLCipher 解密、临时目录管理 |
| `src/db_service.py` | 对解密后 SQLite 的查询封装（会话、消息、联系人、统计） |
| `src/image_store.py` | 聊天图片 `.dat` 的解密、缩略图生成与缓存 |
| `src/emoticon_store.py` | 表情缓存读取、缺失补全 |
| `src/models.py` | 会话与消息数据模型、类型映射 |

## 启动流程

```
main() → ChatViewer.__init__()
   ├─ 构建主题与两个页面（wizard / viewer）
   ├─ after(100ms) 事件轮询
   └─ after(150ms) 自动检测
        └─ 后台线程：find_weixin_process / list_accounts
             └─ event_queue: wechat_detected
                  └─ 确认弹窗 → 后台解密 → event_queue: decrypt_done
                       └─ 读取会话 → 加载消息 → 进入查看器
```

主线程只做 UI，所有耗时操作（内存扫描、解密、图片处理、统计）都在后台线程，
结果通过 `queue.Queue` 回到主线程处理，避免阻塞 Tk 事件循环。

## 密钥提取（关键）

微信不同版本的密钥存放形式不同，`wxdecrypt.extract_keys()` 按以下顺序尝试：

1. **Config.Cipher 路径（微信 4.1.x）**
   - 在微信进程内存中搜索 `com.Tencent.WCDB.Config.Cipher`
   - 以「地址 + 长度」二元组反查引用节点
   - 从节点取配置对象，读其偏移 `+0x88` 处的数据块
   - 用固定掩码 `CONFIG_XOR_MASK` 异或，得到形如 `x'<32B key><16B salt>'` 的字面量
2. **旧格式路径（微信 4.0.x）**
   - 直接扫描 `x'<64位hex>'`

两条路径都不依赖 DLL 版本偏移，差异只在密钥容器形态。

## 数据库解密

- 算法：SQLCipher 4（AES-256-CBC，页大小 4096，reserve 80 = IV 16 + HMAC 64）
- 密钥：32 字节裸 key，或 48 字节 `key + 显式 salt`
- 校验：先只读首页，用 `PBKDF2-HMAC-SHA512(key, salt^0x3A, 2)` 派生 HMAC key，
  校验通过后再读整库解密
- 输出：`<name>.decrypted.db`，写入临时目录并保持相对路径

先校验后整读，是为了在「多候选密钥 × 多数据库」时避免重复读取上百 MB。

## 临时数据生命周期

```
启动 → tempfile.mkdtemp("wx_viewer_*")
解密 → 全部 .decrypted.db 写入该目录
退出 → _on_close() 中 shutil.rmtree 删除
```

程序崩溃时可能残留空目录，属可接受情况（不含明文数据）。

## 线程模型

| 线程 | 任务 |
|---|---|
| 主线程 | Tk 事件循环、绘制 |
| 检测线程 | 进程/账号/版本侦测 |
| 解密线程 | 内存扫描 + 数据库解密 |
| 加载线程 | 会话与消息查询 |
| 媒体线程 | 图片解密、表情补全、头像加载 |
| 看门狗 | 监控主线程心跳，冻结时写 `wx_viewer_hang.log` |
