# YAMNet local classifier

- `yamnet.onnx` is SHA-256 `d3835ffbbd4a1bb3e777f0ca217b5007907f5171dd5d17c4236b95b2af8f908e`, 16,093,355 bytes. It is the fixed commit `f25b741c2f0bdc6d7e6db24b5fddda23347dbafd` of `audiomagic/yamnet-onnx`, whose documented conversion is `tf2onnx 1.16.1` from Google's official TF Hub `google/yamnet/1` SavedModel. Its Apache-2.0 text is retained as `YAMNET_APACHE-2.0.txt`.
- Upstream model architecture, weights provenance and class-map source: TensorFlow Models YAMNet (`research/audioset/yamnet`), Apache-2.0. YAMNet predicts AudioSet's 521 classes from 16 kHz mono waveforms.
- `yamnet_class_map.csv` is copied verbatim from TensorFlow Models YAMNet, SHA-256 `cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2`. The YAMNet repository is Apache-2.0; no AudioSet ontology is packaged.
- Inference uses the already-packaged ONNX Runtime Web 1.22.0 (`../vad/`), MIT; no second runtime is shipped. Its WASM execution provider supports this model's standard ONNX opset 15 graph: Add, Cast, Ceil, Concat, Conv, Div, Gather, GlobalAveragePool, Log, MatMul, Max, Mul, Pad, Pow, Range, Relu, Reshape, Shape, Sigmoid, Slice, Split, Sqrt, Squeeze, Sub, Transpose and Unsqueeze.

The non-official ONNX conversion is pinned, checksummed and documented above; it is not downloaded at runtime. Before a production enforcement release, compare it against the official SavedModel on fixed audio fixtures and real Chrome.
