# fastNLP

[![Build Status](https://travis-ci.org/fastnlp/fastNLP.svg?branch=master)](https://travis-ci.org/fastnlp/fastNLP)
[![codecov](https://codecov.io/gh/fastnlp/fastNLP/branch/master/graph/badge.svg)](https://codecov.io/gh/fastnlp/fastNLP)
[![Pypi](https://img.shields.io/pypi/v/fastNLP.svg)](https://pypi.org/project/fastNLP)
![Hex.pm](https://img.shields.io/hexpm/l/plug.svg)
[![Documentation Status](https://readthedocs.org/projects/fastnlp/badge/?version=latest)](http://fastnlp.readthedocs.io/?badge=latest)

fastNLP 是一款轻量级的 NLP 处理套件。你既可以使用它快速地完成一个序列标注（[NER](reproduction/seqence_labelling/ner/)、POS-Tagging等）、中文分词、文本分类、[Matching](reproduction/matching/)、指代消解、摘要等任务； 也可以使用它构建许多复杂的网络模型，进行科研。它具有如下的特性：

- 统一的Tabular式数据容器，让数据预处理过程简洁明了。内置多种数据集的DataSet Loader，省去预处理代码;
- 多种训练、测试组件，例如训练器Trainer；测试器Tester；以及各种评测metrics等等;
- 各种方便的NLP工具，例如预处理embedding加载（包括EMLo和BERT）; 中间数据cache等;
- 详尽的中文[文档](https://fastnlp.readthedocs.io/)、教程以供查阅;
- 提供诸多高级模块，例如Variational LSTM, Transformer, CRF等;
- 在序列标注、中文分词、文本分类、Matching、指代消解、摘要等任务上封装了各种模型可供直接使用; [详细链接](reproduction/)
- 便捷且具有扩展性的训练器; 提供多种内置callback函数，方便实验记录、异常捕获等。


## 安装指南

fastNLP 依赖如下包:

+ numpy>=1.14.2
+ torch>=1.0.0
+ tqdm>=4.28.1
+ nltk>=3.4.1
+ requests

其中torch的安装可能与操作系统及 CUDA 的版本相关，请参见 [PyTorch 官网](https://pytorch.org/) 。 
在依赖包安装完成后，您可以在命令行执行如下指令完成安装

```shell
pip install fastNLP
```


## 参考资源

- [文档](https://fastnlp.readthedocs.io/zh/latest/)
- [源码](https://github.com/fastnlp/fastNLP)



## 内置组件

大部分用于的 NLP 任务神经网络都可以看做由编码（encoder）、聚合（aggregator）、解码（decoder）三种模块组成。


![](./docs/source/figures/text_classification.png)

fastNLP 在 modules 模块中内置了三种模块的诸多组件，可以帮助用户快速搭建自己所需的网络。 三种模块的功能和常见组件如下:

<table>
<tr>
    <td><b> 类型 </b></td>
    <td><b> 功能 </b></td>
    <td><b> 例子 </b></td>
</tr>
<tr>
    <td> encoder </td>
    <td> 将输入编码为具有具 有表示能力的向量 </td>
    <td> embedding, RNN, CNN, transformer
</tr>
<tr>
    <td> aggregator </td>
    <td> 从多个向量中聚合信息 </td>
    <td> self-attention, max-pooling </td>
</tr>
<tr>
    <td> decoder </td>
    <td> 将具有某种表示意义的 向量解码为需要的输出 形式 </td>
    <td> MLP, CRF </td>
</tr>
</table>


## 完整模型
fastNLP 为不同的 NLP 任务实现了许多完整的模型，它们都经过了训练和测试。

你可以在以下两个地方查看相关信息
- [模型介绍](reproduction/)
- [模型源码](fastNLP/models/)

## 项目结构

![](./docs/source/figures/workflow.png)

fastNLP的大致工作流程如上图所示，而项目结构如下：

<table>
<tr>
    <td><b> fastNLP </b></td>
    <td> 开源的自然语言处理库 </td>
</tr>
<tr>
    <td><b> fastNLP.core </b></td>
    <td> 实现了核心功能，包括数据处理组件、训练器、测试器等 </td>
</tr>
<tr>
    <td><b> fastNLP.models </b></td>
    <td> 实现了一些完整的神经网络模型 </td>
</tr>
<tr>
    <td><b> fastNLP.modules </b></td>
    <td> 实现了用于搭建神经网络模型的诸多组件 </td>
</tr>
<tr>
    <td><b> fastNLP.io </b></td>
    <td> 实现了读写功能，包括数据读入，模型读写等 </td>
</tr>
</table>


<hr>

*In memory of @FengZiYjun.  May his soul rest in peace. We will miss you very very much!*
