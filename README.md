# 仿Visual Studio OpenCV 插件Image Watch

![image-20251016100805917](C:\Users\TK\AppData\Roaming\Typora\typora-user-images\image-20251016100805917.png)

# 0 功能

1. 支持查看图像像素值
2. 支持实时显示鼠标指针位置像素值和指针位置
3. 鼠标滚轮放大缩小图像
4. 按住鼠标左键拖拽图像
5. Shift+鼠标滚轮，左右平移图像
6. Ctrl+鼠标滚轮，上下平移图像
7. 鼠标左键双击，图像恢复自适应窗口尺寸

# 1 依赖

- python 3.8
- pyside6

# 2 打包

```shell
pyside6-deploy.exe .\main.py
# 修改生成的spec文件中的nuitka模块，取消控制台窗口
# [nuitka]
# # 可选: 指定 standalone（生成带依赖的目录）或 onefile（打包成单文件）
# # 推荐 standalone 来避免 onefile 的短暂解包 / 闪烁与调试困难
# mode = standalone

# # 关键：传给 Nuitka 的额外参数
# # 1) 禁用控制台（Windows GUI 程序）：--windows-disable-console
# # 2) 把图标传给 Nuitka（可选）：--windows-icon-from-ico=<path-to-ico>
# # 3) 保留原有 quiet/noinclude 的设置
# extra_args = --quiet --noinclude-qt-translations --windows-disable-console --windows-icon-from-ico=C:\anaconda3\envs\openmmlab-pyqt-py38\Lib\site-packages\PySide6\scripts\deploy_lib\pyside_icon.ico

pyside6-deploy.exe -c .\pysidedeploy.spec
```

> Powered By ChatGPT5 Thinking Mini