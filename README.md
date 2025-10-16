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
pyside6-deploy.exe -c .\pysidedeploy.spec
```

> Powered By ChatGPT5 Thinking Mini