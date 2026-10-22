# 构建与发布

## 环境要求

- Windows 10/11 x64
- Python 3.11
- PyInstaller 6.x

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pyinstaller
```

## 构建

```powershell
.\scripts\build.ps1
```

输出：`dist\微信聊天记录查看器\`（目录版，含 `_internal\`）

采用 **onedir** 而非 onefile：启动约 1.1 秒，onefile 需先解压到 `%TEMP%\_MEI*`，
实测慢 4 倍以上且会残留临时目录。

## 打包发布

```powershell
.\scripts\release.ps1
```

输出：`release\微信聊天记录查看器-v<版本>-win64.zip`，内含：

```
微信聊天记录查看器.exe
_internal\
启动查看器.bat
README.md
VERSION
```

## 版本发布流程

1. 修改 `VERSION`
2. 更新 `CHANGELOG.md`
3. 提交：`git commit -am "release: v1.0.0"`
4. 打标签：`git tag -a v1.0.0 -m "v1.0.0"`
5. 推送：`git push && git push --tags`
6. 打包：`.\scripts\release.ps1`
7. 发布：`gh release create v1.0.0 "release\...zip" --title "v1.0.0" --notes-file CHANGELOG.md`

## 注意事项

- `启动查看器.bat` 为 **GBK 编码 + CRLF**。用 UTF-8 保存会导致中文路径下
  cmd 解析失败、程序不启动。`.gitattributes` 已将该文件标记为 `-text`。
- `dist/`、`build/`、`release/` 均不入库；可执行文件通过 GitHub Release 分发
  （单文件超过 100 MB 不能进入 git 历史）。
- 若新增第三方依赖，需要同步更新 `requirements.txt` 与 `scripts/build.ps1`
  中的 `--hidden-import`。
