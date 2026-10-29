# 打包与发版说明

本项目使用 [PyInstaller](https://pyinstaller.org/) 打包为 Windows 免安装版（onedir）。

## 1. 准备环境

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller
```

## 2. 一键打包

```powershell
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

脚本会做三件事：

1. 调用 PyInstaller 生成 `dist\AIOps_Agent\`（含 `AIOps_Agent.exe` 与 `_internal\`）；
2. 把 `src\index.html` 作为资源打进包里（运行时通过 `sys._MEIPASS` 读取）；
3. 压缩为 `dist\AIOps_Agent.zip`。

产物约 65–70 MB。

## 3. 发布到 GitHub Release

```powershell
gh release create v1.0.1 dist\AIOps_Agent.zip `
  --title "v1.0.1 · AIOps_Agent" `
  --notes "自然语言 SSH 运维、多主机分组、值守监控与邮件告警、本地知识库。"
```

没有 `gh` 的话，直接在仓库页面 **Releases → Draft a new release** 手动上传 zip 即可。

## 4. 注意事项

- 打包版首次运行会在 exe 同目录生成 `system.db`、`secret.key`、`logs/`、`uploads_images/`，**升级时保留这些文件**，否则配置全丢。
- 解压路径避免中文与空格，某些 Windows 环境下 PyInstaller 对非 ASCII 路径兼容性一般。
- 不要打包 `.venv`、`dist`、`system.db`、`secret.key` 到仓库里，`.gitignore` 已排除。
- 若杀毒软件误报（PyInstaller 常见），可对 exe 做代码签名，或在 Release 说明里提示用户添加信任。
