# 环校集市

校园二手交易的本机 Web 应用。使用 Python 标准库、SQLite 和原生 HTML、CSS、JavaScript。

## 启动

在本目录打开终端，执行下面两行。需要 Python 3.10 或更高版本，无运行时第三方依赖。

    cd code
    python -m campus_trade serve

用桌面浏览器打开 http://127.0.0.1:8765/ 。默认从空的本地数据库开始，运行数据保存在 code/var/。终端按 Ctrl+C 停止服务，数据保留。端口占用时可执行 python -m campus_trade serve --port 8766。

游客可以浏览已发布商品。点击登录进入表单，随后可点注册。验证码由本地演示界面显示，无须真实手机号或短信服务。普通账号可发布和预约商品。

## 创建首个超级管理员

先在 code 目录执行下列命令，再按提示在终端输入自己的密码。示例号码可改成其他未注册的 11 位号码。

    python -m campus_trade create-super --phone 13800000000

登录该账号后，可在账号管理创建普通管理员。新管理员首次登录必须修改初始密码。卖家提交商品后，用管理员账号通过审核即可让商品进入公开集市。用另一个普通账号预约，卖家确认后任意一方可标记交易完成。

## 构建与测试

在 code 目录执行

    python build.py

脚本编译 Python，统计应用源代码行数，运行 unittest。若已安装 Node.js，还会检查 JavaScript 语法。加 --archive dist/campus-trade.zip 可生成运行源码归档，解压后用同样的方式启动。

风格检查使用 Ruff，规则为 E、F、I，并由格式化器处理一般换行。

    ruff check --select E,F,I --ignore E501 .
    ruff format --check --line-length 99 .

## 实现入口

| 职责 | 路径 |
| --- | --- |
| 请求接收序、事务、权限和数据库 | code/campus_trade/server.py、core.py、core_schema.sql |
| 登录、注册、会话与管理员创建 | code/campus_trade/accounts.py |
| 商品、查询、图片、送审、举报和整改 | code/campus_trade/products.py、product_schema.sql |
| 预约状态、幂等回执、超时和违规联动 | code/campus_trade/reservations.py、reservation_schema.sql |
| 统计、日志和通知 | code/campus_trade/operations.py |
| 桌面浏览器页面 | code/campus_trade/static/ |
| 实验二设计图 | UML/ |

SQLite 存储金额使用整数分，日期按北京时间展示。图片作为 BLOB 与商品资料同时保存，读取时复查可见范围。单进程服务按完整写请求到达应用的顺序执行，SQLite 事务及唯一索引保护有效预约。实际交接与付款在线下进行。账号头像使用内置标记，验证码使用本地演示通道。

## 课程归档

在仓库根目录运行 python pack_submission.py --report <本人撰写的PDF路径> 。归档脚本会把指定 PDF、四种 UML 的源码和图片、可运行代码打进课程指定目录，排除数据库和缓存。
