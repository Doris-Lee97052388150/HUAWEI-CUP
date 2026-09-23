# 2026 华为杯数学建模论文 LaTeX 模板

按第二十三届官方模板及 2026 年论文格式规范整理，对应 Word 连续排版修正版。保留封面四个标志、赛事名称、学校、队号及三名队员填写区；第二页起匿名。正文不设置不必要的强制分页。所有【】均为待填写提示，不是已完成的研究结论。

## 开始写作

1. 编辑 `config.tex`：学校、队号、姓名、论文题目及关键词。身份字段仅输出在封面，不写进 PDF 作者元数据。
2. 编辑 `sections/abstract.tex`：中文摘要，通常不超过两页，无需英文摘要。
3. 编辑 `sections/body.tex`：正文。按实际问题数增删章节；标题、公式、图表与交叉引用自动编号。
4. 编辑 `sections/references.tex`：将全部占位文献替换为真实阅读并核验过的文献；条目按正文首次引用顺序排列。当前提供手工文献列表，无需 BibTeX 或 Biber。
5. 编辑 `sections/appendix.tex`：复现材料与真实 AI 使用记录。

## 本地编译

安装 TeX Live 或 MacTeX，使用 **XeLaTeX**，不要使用 pdfLaTeX。推荐在本目录运行：

```sh
latexmk -xelatex main.tex
```

没有 latexmk 时，连续执行两遍：

```sh
xelatex -interaction=nonstopmode -halt-on-error main.tex
xelatex -interaction=nonstopmode -halt-on-error main.tex
```

输出 `main.pdf`。两遍编译用于解析 `\ref`、`\eqref` 和 `\cite`。也可使用兼容 XeTeX 的 Tectonic：`tectonic main.tex`，其首次运行需要联网获取依赖。

## Overleaf

新建项目并上传压缩包，主文件选择 `main.tex`，编译器选择 **XeLaTeX**。根目录的 `main.tex`、`setup.tex`、`fonts.tex`、`config.tex` 与 `sections/`、`assets/` 需保持相对位置。

若在 Overleaf 免费版遇到编译超时，可用 `fonts-overleaf.tex` 的内容替换在线项目的 `fonts.tex`。该配置直接加载字体文件，避免查找服务器未安装的系统字体；仍优先使用 `fonts/` 内的宋体、黑体文件。当前在线项目已应用此配置，并在 XeLaTeX、TeX Live 2025 下成功生成 6 页预览。本地默认 `fonts.tex` 保留原有系统字体识别。在线预览缺少宋体、黑体时仍使用 Fandol；正式提交前须配置规定字体。

字体会优先使用系统宋体 SimSun 和黑体 SimHei；也可将你合法持有的字体文件分别放到 `fonts/simsun.ttc` 与 `fonts/simhei.ttf`。字体文件不随模板分发。Overleaf 通常没有这两种字体，缺失时模板使用 Fandol 预览，并在编译日志中提示；**Fandol 预览不等同于已满足官方宋体、黑体要求**，正式提交前应配置要求的字体重新编译。

赛事抬头优先用华文新魏，缺失时采用黑体。该替代只涉及封面与赛事抬头；若需严格匹配官方字形，应在有合法华文新魏字体的环境下编译。正文汉字小四宋体，题目三号黑体，一级标题四号黑体居中。英文字体优先 Times New Roman，缺失时使用 TeX Gyre Termes。

## 图表、公式与引用

将图像放在 `assets/` 或自建的 `figures/` 文件夹，把 `\FigurePlaceholder{...}` 替换成：

```tex
\includegraphics[width=0.85\linewidth]{assets/your-figure.pdf}
```

保留 `\caption` 和后面的 `\label`。用 `图~\ref{fig:results}`、`表~\ref{tab:results}` 引用，用 `式~\eqref{eq:model}` 引用公式。优先使用清晰的 PDF 矢量图，或清晰的 PNG 图片。图表宽度不要超过 `\linewidth`。

使用 `\begin{equation} ... \end{equation}` 输入带编号公式，不手打公式序号。三线表使用 `booktabs`，长表可按实际需要改用 `longtable`。默认浮动体在相关段落附近排版，`\FloatBarrier` 限制其漂移；遇到大图或长表，允许自然换页，不用连续空行挤位置。

文献用 `\cite{文献键}` 引用，书籍引用处还应注明页码。`references.tex` 中条目顺序需要自行按首次引用整理。模板中的书籍、中文期刊、英文期刊和网络资源条目全部是格式占位，**没有虚构的作者或论文**。

LaTeX 特殊字符 `%`、`&`、`_`、`#` 在普通文字中需写成 `\%`、`\&`、`\_`、`\#`；网址使用 `\url{...}`。

## 提交检查

- 清除全部【】占位以及格式示例公式，确认结果来自实际求解，文献可查证。
- 封面不编号，摘要从 1 编号，正文接续；没有页眉。第二页起不得出现学校、姓名、队号和其他身份信息。
- PDF 文档属性不写参赛队身份。检查图像、代码路径、批注和附件，避免身份泄漏。
- 摘要、正文、图表、结论中的结果、单位和有效数字保持一致；检查无 `??`、缺字、越界和未定义引用。
- 使用 AI 时按今年规定填写工具信息；在辅助数据分析结果附近和程序前进行相应标注，附录不替代局部说明。
- 将最终 PDF 按题号字母加队号命名，例如官方示例 `A26000010001.pdf`；提交 MD5 后不能再次改动该 PDF。
- 官方附件未统一规定正文页数上限；以赛题和当届后续通知为准。

`main.pdf` 是模板预览，不是可直接提交的完成论文。

## 来源

- [2026 年开赛公告及附件](https://cpipc.acge.org.cn/cw/contestNews/detail/4/2c90801ca06f016101a0a47e06b77d9b?page=0)
- [官方论文模板](https://cpipc.acge.org.cn/sysFile/downFile.do?fileId=a730b312331e492baad17b248bad6b51)
- [论文格式规范](https://cpipc.acge.org.cn/sysFile/downFile.do?fileId=97a93e3a9e074738aea95eea986508f2)
- [人工智能工具及输出使用规定](https://cpipc.acge.org.cn/sysFile/downFile.do?fileId=4fc024f7a1504abb87fc056440c64a28)

`assets/` 标志取自官方 Word 模板；西安交通大学标志按官方模板已有裁剪范围提取，未重新设计或替换。使用时保留四个标志。此 LaTeX 版本为依据官方底稿整理的便用版本，不是组委会另行发布的 LaTeX 文件。
