#
# Copyright (c) 2022 TUM Department of Electrical and Computer Engineering.
#
# This file is part of MLonMCU.
# See https://github.com/tum-ei-eda/mlonmcu.git for further info.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import re
import time
import tempfile
import multiprocessing
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Tuple, List

from mlonmcu.feature.features import get_matching_features
from mlonmcu.models.model import ModelFormats, Model
from mlonmcu.feature.type import FeatureType
from mlonmcu.config import filter_config, str2bool
from mlonmcu.artifact import Artifact, ArtifactFormat
from mlonmcu.setup import utils
from mlonmcu.target.metrics import Metrics

from mlonmcu.logging import get_logger


logger = get_logger()


class Frontend(ABC):
    FEATURES = ["validate"]

    DEFAULTS = {
        "use_inout_data": False,
        # TODO: print_outputs for frontends
    }

    REQUIRED = []
    OPTIONAL = []

    def __init__(self, name, input_formats=None, output_formats=None, features=None, config=None):
        self.name = name
        self.input_formats = input_formats if input_formats else []
        self.output_formats = output_formats if output_formats else []
        self.config = config if config else {}
        self.features = self.process_features(features)
        self.config = filter_config(self.config, self.name, self.DEFAULTS, self.OPTIONAL, self.REQUIRED)

    def __repr__(self):
        probs = []
        if self.name:
            probs.append(self.name)
        if self.features and len(self.features) > 0:
            probs.append(str(self.features))
        if self.config and len(self.config) > 0:
            probs.append(str(self.config))
        return "Frontend(" + ",".join(probs) + ")"

    @property
    def use_inout_data(self):
        value = self.config["use_inout_data"]
        return str2bool(value) if not isinstance(value, (bool, int)) else value

    def supports_formats(self, ins=None, outs=None):
        """Returs true if the frontend can handle at least one combination of input and output formats."""
        assert ins is not None or outs is not None, "Please provide a list of input formats, outputs formats or both"
        ret = True
        if ins:
            if not isinstance(ins, list):
                ins = [ins]
            supported = any(fmt in self.input_formats for fmt in ins)
            ret = ret and supported
        if outs:
            if not isinstance(outs, list):
                outs = [outs]
            supported = any(fmt in self.output_formats for fmt in ins)
            ret = ret and supported
        return ret

    def process_features(self, features):
        if features is None:
            return []
        features = get_matching_features(features, FeatureType.FRONTEND)
        for feature in features:
            assert (  # If this assertion occurs, continue with the next frontend instead of failing
                # (TODO: create custom exception type)
                feature.name
                in self.FEATURES
            ), f"Incompatible feature: {feature.name}"
            # Instead we might introduce self.compatible and set it to true at this line
            feature.used = True
            feature.add_frontend_config(self.name, self.config)
            feature.update_formats(self.name, self.input_formats, self.output_formats)
        return features

    @abstractmethod
    # def produce_artifacts(self, model):
    def produce_artifacts(self, model):
        pass

    def process_metadata(self, model, cfg=None):
        model_dir = Path(model.paths[0]).parent.resolve()
        metadata = model.metadata
        in_paths = []
        out_paths = []
        if self.use_inout_data:
            if metadata is not None and "network_parameters" in metadata:
                network = metadata["network_parameters"]
                assert "input_nodes" in network
                ins = network["input_nodes"]
                for inp in ins:
                    if "example_input" in inp and "path" in inp["example_input"]:
                        in_data_dir = Path(inp["example_input"]["path"])
                        # TODO: this will only work with relative paths to model dir! (Fallback to parent directories?)
                        in_path = model_dir / in_data_dir
                        assert (
                            in_path.is_dir()
                        ), f"Input data directory defined in model metadata does not exist: {in_path}"
                        in_paths.append(in_path)
                assert "output_nodes" in network
                outs = network["output_nodes"]
                for outp in outs:
                    if "test_output_path" in outp:
                        out_data_dir = Path(outp["test_output_path"])
                        out_path = model_dir / out_data_dir
                        assert (
                            in_path.is_dir()
                        ), f"Output data directory defined in model metadata does not exist: {out_path}"
                        out_paths.append(out_path)
            else:
                fallback_in_path = model_dir / "input"
                if fallback_in_path.is_dir():
                    in_paths.append(fallback_in_path)
                fallback_out_path = model_dir / "output"
                if fallback_out_path.is_dir():
                    out_paths.append(fallback_out_path)

        if metadata is not None and "backends" in metadata:
            assert cfg is not None
            backend_options = metadata["backends"]
            for backend in backend_options:
                if backend_options[backend] is not None:
                    flattened = {f"{backend}.{key}": value for key, value in backend_options[backend].items()}
                    cfg.update(flattened)

        # Detect model support code (Allow overwrite in metadata YAML)
        support_path = model_dir / "support"
        if support_path.is_dir():
            assert cfg is not None
            # TODO: onlu overwrite if unset?
            cfg.update({"mlif.model_support_dir": support_path})
            # cfg.update({"espidf.model_support_dir": support_path})
            # cfg.update({"zephyr.model_support_dir": support_path})
        if len(in_paths) > 0:
            cfg.update({"mlif.input_data_path": in_paths})
            # cfg.update({"espidf.input_data_path": in_paths})
            # cfg.update({"zephyr.input_data_path": in_paths})
        if len(out_paths) > 0:
            cfg.update({"mlif.output_data_path": out_paths})
            # cfg.update({"espidf.output_data_path": out_paths})
            # cfg.update({"zephyr.output_data_path": out_paths})

    def generate(self, model) -> Tuple[dict, dict]:
        artifacts = []

        count = len(model.paths)
        assert count == len(model.formats)
        assert count > 0, f"'{self.name}' frontend expects at least one model"
        max_ins = len(self.input_formats)
        assert count <= max_ins, f"'{self.name}' frontend did not expect more than {max_ins} models"
        formats = model.formats
        assert self.supports_formats(formats), f"Invalid model format for '{self.name}' frontend"

        artifacts = self.produce_artifacts(model)
        if not isinstance(artifacts, list):
            artifacts = [artifacts]
        assert len(artifacts) > 0, f"'{self.name}' frontend should produce at least one model"
        max_outs = len(self.output_formats)
        assert len(artifacts) <= max_outs, f"'{self.name}' frontend should not return more than {max_outs}"

        # If we want to use the same instance of this Frontend in parallel, we need to get rid of self.artifacts...
        return {"default": artifacts}, {}

    def generate_artifacts(self, model) -> List[Artifact]:
        start_time = time.time()
        artifacts, metrics = self.generate(model)
        # TODO: do something with out?
        end_time = time.time()
        diff = end_time - start_time
        if len(metrics) == 0:
            metrics = {"default": Metrics()}
        for name, metrics_ in metrics.items():
            if name == "default":
                metrics_.add("Load Stage Time [s]", diff, True)
            content = metrics_.to_csv(include_optional=True)
            artifact = Artifact("load_metrics.csv", content=content, fmt=ArtifactFormat.TEXT, flags=["metrics"])
            if name not in artifacts:
                artifacts[name] = []
            artifacts[name].append(artifact)
        self.artifacts = artifacts
        return artifacts

    def export_artifacts(self, path):
        assert len(self.artifacts) > 0, "No artifacts found, please run generate_artifacts() first"

        if not isinstance(path, Path):
            path = Path(path)

        is_dir = len(path.suffix) == 0
        if is_dir:
            assert (
                path.is_dir()
            ), "The supplied path does not exists."  # Make sure it actually exists (we do not create it by default)
            for artifact in self.artifacts:
                artifact.export(path)
        else:
            raise NotImplementedError


class SimpleFrontend(Frontend):
    """An abstract frontend with equivalent input and output formats."""

    # Assumption: only raw model data
    def __init__(self, name, fmt, features=None, config=None):
        super().__init__(
            name,
            input_formats=[fmt],
            output_formats=[fmt],
            features=features,
            config=config,
        )

    def produce_artifacts(self, model):
        assert len(self.input_formats) == len(self.output_formats) == len(model.paths) == 1
        artifacts = []
        name = model.name
        path = model.paths[0]
        ext = self.input_formats[0].extension
        with open(path, "rb") as handle:  # TODO: is an onnx model raw data or text?
            raw = handle.read()
            artifacts.append(Artifact(f"{name}.{ext}", raw=raw, fmt=ArtifactFormat.RAW), flags=["model"])
        return artifacts


# TODO: move to frontends.py
# TODO: frontend parsed metadata instead of lookup.py?
# TODO: how to find inout_data?
class TfLiteFrontend(SimpleFrontend):
    FEATURES = Frontend.FEATURES + ["visualize", "split_layers"]

    DEFAULTS = {
        **Frontend.DEFAULTS,
        "visualize_enable": False,
        "visualize_script": None,
        "split_layers": False,
        "pack_script": None,
    }

    REQUIRED = Frontend.REQUIRED + []

    def __init__(self, features=None, config=None):
        super().__init__(
            "tflite",
            ModelFormats.TFLITE,
            features=features,
            config=config,
        )

    @property
    def visualize_enable(self):
        value = self.config["visualize_enable"]
        return str2bool(value) if not isinstance(value, (bool, int)) else value

    @property
    def split_layers(self):
        value = self.config["split_layers"]
        return str2bool(value) if not isinstance(value, (bool, int)) else value

    @property
    def visualize_script(self):
        return self.config["visualize_script"]

    @property
    def pack_script(self):
        return self.config["pack_script"]

    def produce_artifacts(self, model):
        assert len(self.input_formats) == len(model.paths) == 1
        artifacts = []

        name = model.name
        path = model.paths[0]
        ext = self.input_formats[0].extension
        with open(path, "rb") as handle:
            raw = handle.read()
            artifacts.append(Artifact(f"{name}.{ext}", raw=raw, fmt=ArtifactFormat.RAW), flags=["model"])

        if not self.visualize_enable:
            assert len(self.output_formats) == 1
        else:
            assert len(self.output_formats) == 2

            assert self.visualize_script is not None

            in_file = model.paths[0]
            ext = "html"
            with tempfile.TemporaryDirectory() as tmpdirname:
                out_file = str(Path(tmpdirname) / f"tflite_visualize.{ext}")

                utils.python(self.visualize_script, in_file, out_file, print_output=False)

                with open(out_file, "r") as handle:
                    tflite_visualize_text = handle.read()

                tflite_visualize_artifact = Artifact(
                    f"tflite_visualize.{ext}",
                    content=tflite_visualize_text,
                    fmt=ArtifactFormat.TEXT,
                )
                artifacts.append(tflite_visualize_artifact)

        return artifacts

    def generate(self, model) -> Tuple[dict, dict]:
        if self.split_layers:
            artifacts = {}

            name = model.name
            path = model.paths[0]
            formats = model.formats
            assert self.supports_formats(formats), f"Invalid model format for '{self.name}' frontend"

            ret = self.produce_artifacts(model)
            if not isinstance(ret, list):
                ret = [ret]
            assert len(ret) > 0, f"'{self.name}' frontend should produce at least one model"
            max_outs = len(self.output_formats)
            assert len(ret) <= max_outs, f"'{self.name}' frontend should not return more than {max_outs}"
            artifacts["default"] = ret
            with tempfile.TemporaryDirectory() as tmpdirname:

                def get_num_layers(file):
                    tflite_pack_args = [path, "--count-layers", "--noop"]
                    out = utils.exec_getout(self.pack_script, *tflite_pack_args, print_output=False)
                    matches = re.compile(r"Found\s(\d+)\slayers.").findall(out)
                    assert len(matches) == 1
                    num = int(matches[0])
                    return num

                def gen_layer_files(file, dest):
                    results = []
                    num_layers = get_num_layers(file)
                    assert num_layers > 0
                    for i in range(num_layers):
                        out_name = f"layer{i}"
                        out_file = Path(dest) / out_name
                        tflite_pack_args = [path, "-k", str(i), "--out", out_file]
                        utils.exec_getout(self.pack_script, *tflite_pack_args, print_output=False)
                        assert out_file.is_file()
                        results.append(out_file)
                    return results

                layer_files = gen_layer_files(path, tmpdirname)

                for i, layer_file in enumerate(layer_files):
                    subrun = f"layer{i}"
                    layer_name = f"{name}_{subrun}"
                    layer_model = Model(layer_name, [layer_file])
                    ret = self.produce_artifacts(layer_model)
                    if not isinstance(ret, list):
                        ret = [ret]
                    assert len(ret) > 0, f"'{self.name}' frontend should produce at least one model"
                    max_outs = len(self.output_formats)
                    assert len(ret) <= max_outs, f"'{self.name}' frontend should not return more than {max_outs}"
                    artifacts[subrun] = ret

            return artifacts, {}
        else:
            return super().generate(model)


class RelayFrontend(SimpleFrontend):
    FEATURES = Frontend.FEATURES + ["relayviz"]

    DEFAULTS = {**Frontend.DEFAULTS, "visualize_graph": False, "relayviz_plotter": "term"}

    REQUIRED = Frontend.REQUIRED + ["tvm.build_dir", "tvm.pythonpath"]

    def __init__(self, features=None, config=None):
        super().__init__(
            "relay",
            ModelFormats.RELAY,
            features=features,
            config=config,
        )

    @property
    def visualize_graph(self):
        value = self.config["visualize_graph"]
        return str2bool(value) if not isinstance(value, (bool, int)) else value

    @property
    def relayviz_plotter(self):
        return self.config["relayviz_plotter"]

    @property
    def tvm_build_dir(self):
        return self.config["tvm.build_dir"]

    @property
    def tvm_pythonpath(self):
        return self.config["tvm.pythonpath"]

    def produce_artifacts(self, model):
        assert len(self.input_formats) == len(model.paths) == 1
        artifacts = []

        name = model.name
        path = model.paths[0]
        ext = self.input_formats[0].extension
        with open(path, "rb") as handle:  # TODO: is an onnx model raw data or text?
            raw = handle.read()
            artifacts.append(Artifact(f"{name}.{ext}", raw=raw, fmt=ArtifactFormat.RAW), flags=["model"])

        if not self.visualize_graph:
            assert len(self.output_formats) == 1
        else:
            assert len(self.output_formats) == 2

            def _relayviz(in_file, out_file, plotter_name, env={}):
                import sys
                import os

                sys.path.append(env["PYTHONPATH"])
                os.environ["TVM_LIBRARY_PATH"] = env["TVM_LIBRARY_PATH"]
                from tvm import parser
                from tvm.contrib import relay_viz
                from tvm.contrib.relay_viz.terminal import TermPlotter
                from tvm.contrib.relay_viz.dot import DotPlotter

                if plotter_name == "term":
                    plotter_cls = TermPlotter
                elif plotter_name == "dot":
                    plotter_cls = DotPlotter
                else:
                    raise RuntimeError(f"Invalid plotter name: {plotter_name}")

                with open(in_file, "r", encoding="utf-8") as relay_text:
                    text = relay_text.read()

                mod = parser.fromtext(text)

                plotter_inst = plotter_cls()
                viz = relay_viz.RelayVisualizer(mod, plotter=plotter_inst)
                out_file_base = os.path.splitext(out_file)[0]
                viz.render(filename=out_file_base)

            in_file = model.paths[0]
            ext = "" if self.relayviz_plotter == "term" else "pdf"
            with tempfile.TemporaryDirectory() as tmpdirname:
                out_file = str(Path(tmpdirname) / (f"relayviz.{ext}" if len(ext) > 0 else "relayviz"))
                proc = multiprocessing.Process(
                    target=_relayviz,
                    args=[in_file, out_file, self.relayviz_plotter],
                    kwargs={"env": {"PYTHONPATH": self.tvm_pythonpath, "TVM_LIBRARY_PATH": self.tvm_build_dir}},
                )
                proc.start()
                proc.join()

                if self.relayviz_plotter == "term":
                    with open(out_file, "r") as handle:
                        relayviz_text = handle.read()

                    relayviz_artifact = Artifact(
                        "relayviz.txt",
                        content=relayviz_text,
                        fmt=ArtifactFormat.TEXT,
                    )
                else:
                    with open(out_file, "rb") as handle:
                        relayviz_data = handle.read()

                    relayviz_artifact = Artifact(
                        f"relayviz.{ext}",
                        raw=relayviz_data,
                        fmt=ArtifactFormat.RAW,
                    )
                artifacts.append(relayviz_artifact)

        return artifacts


class PackedFrontend(Frontend):  # Inherit from TFLiteFrontend? -> how to do constructor?
    FEATURES = Frontend.FEATURES + ["packing", "packed"]

    DEFAULTS = {
        **Frontend.DEFAULTS,
        "ignore_existing": True,
        "fake_pack": False,  # Pretend that every compatible tensor is packable
        # (best case scenerio, TODO: rename to force_pack?)
        "use_packed": True,
        "check": False,  # Unimplemented
    }

    REQUIRED = ["packer.exe"]  # TODO move to feature?

    def __init__(self, features=None, config=None):
        super().__init__(name="packed", features=features, config=config)
        if self.fake_pack or self.ignore_existing:
            # assert self.use_packed
            self.input_formats = [ModelFormats.TFLITE]
        else:
            self.input_formats = [ModelFormats.PACKED, ModelFormats.TFLITE]

        # if self.use_packed:
        self.output_formats = [
            ModelFormats.PACKED,
            ModelFormats.TFLITE,
        ]  # Always copy over the input model as intermediate artifact for reproducability
        # TODO: add a Frontend.DEFAULT which can be used to turn off this behavoir (keep intermediate?)
        # else:
        #     self.output_formats = [ModelFormats.TFLITE]
        # Order of formats ir irrelevant here, hot for artifacts, the first one will always be the main object

    @property
    def ignore_existing(self):
        value = self.config["ignore_existing"]
        return str2bool(value) if not isinstance(value, (bool, int)) else value

    @property
    def fake_pack(self):
        value = self.config["fake_pack"]
        return str2bool(value) if not isinstance(value, (bool, int)) else value

    @property
    def use_packed(self):
        value = self.config["use_packed"]
        return str2bool(value) if not isinstance(value, (bool, int)) else value

    @property
    def check(self):
        value = self.config["check"]
        return str2bool(value) if not isinstance(value, (bool, int)) else value

    def produce_artifacts(self, model):
        tflite_data = None
        packed_data = None
        name = model.name

        if self.fake_pack or self.ignore_existing:  # -> ins: TFLITE
            # assert self.use_packed
            tflite_path = model.paths[0]

            with open(tflite_path, "rb") as handle:
                tflite_data = handle.read()
        else:  # -> ins: TFLITE, PACKED
            for path, fmt in zip(model.paths, model.formats):
                data_in = None
                with open(path, "rb") as handle:
                    data_in = handle.read()
                if fmt == ModelFormats.PACKED:
                    packed_data = data_in
                    break
                elif fmt == ModelFormats.TFLITE:
                    # tflite_data_in = data_in
                    break
                else:
                    raise RuntimeError(f"Unexpected model format: {fmt}")

        if packed_data is None:
            # Do packing
            with tempfile.TemporaryDirectory() as tmpdirname:
                logger.debug("Using temporary directory for packing results: %s", tmpdirname)
                packer_exe = self.config["packer_exe"]
                assert packer_exe is not None
                in_file = Path(tmpdirname) / "in.tflite"
                with open(in_file, "wb") as handle:
                    handle.write(tflite_data)
                out_file = Path(tmpdirname) / "out.tflm"
                utils.exec_getout(packer_exe, in_file, out_file)
                with open(out_file, "rb") as handle:
                    packed_data = handle.read()

        if self.check:
            raise NotImplementedError

        tflite_artifact = Artifact(
            f"{name}.tflite",
            raw=tflite_data,
            fmt=ArtifactFormat.RAW,
            optional=self.use_packed,
            flags=["model"],
        )
        packed_artifact = Artifact(
            f"{name}.tflm",
            raw=packed_data,
            fmt=ArtifactFormat.RAW,
            optional=not self.use_packed,
            flags=["model"],
        )

        if self.use_packed:
            return [packed_artifact, tflite_artifact]
        else:
            return [tflite_artifact, packed_artifact]


class ONNXFrontend(SimpleFrontend):
    FEATURES = Frontend.FEATURES

    DEFAULTS = {
        **Frontend.DEFAULTS,
    }

    REQUIRED = Frontend.REQUIRED + []

    def __init__(self, features=None, config=None):
        super().__init__(
            "onnx",
            ModelFormats.ONNX,
            features=features,
            config=config,
        )


class PBFrontend(SimpleFrontend):
    FEATURES = Frontend.FEATURES

    DEFAULTS = {
        **Frontend.DEFAULTS,
    }

    REQUIRED = Frontend.REQUIRED + []

    def __init__(self, features=None, config=None):
        super().__init__(
            "pb",
            ModelFormats.PB,
            features=features,
            config=config,
        )


class PaddleFrontend(SimpleFrontend):
    FEATURES = Frontend.FEATURES

    DEFAULTS = {
        **Frontend.DEFAULTS,
    }

    REQUIRED = Frontend.REQUIRED + []

    def __init__(self, features=None, config=None):
        super().__init__(
            "paddle",
            ModelFormats.PADDLE,
            features=features,
            config=config,
        )


class LayerGenFrontend(Frontend):
    FEATURES = Frontend.FEATURES

    DEFAULTS = {
        **Frontend.DEFAULTS,
        "fmt": "tflite",  # TODO: relay
    }

    REQUIRED = Frontend.REQUIRED + ["layergen.exe"]

    def __init__(self, features=None, config=None):
        super().__init__(
            "layergen",
            input_formats=[ModelFormats.TEXT],
            output_formats=[ModelFormats.TFLITE, ModelFormats.RELAY],
            features=features,
            config=config,
        )

    @property
    def fmt(self):
        value = self.config["fmt"]
        value = value.upper()
        assert value in ["TFLITE", "RELAY"]
        return value

    @property
    def layergen_exe(self):
        return Path(self.config["layergen.exe"])

    def produce_artifacts(self, model):
        pass

    # def produce_artifacts(self, model):
    #     artifacts = {}
    #     name = model.name
    #     path = model.paths[0]
    #     ext = ModelFormats[self.fmt].extension
    #     print("ext", ext)
    #     with open(path, "r") as handle:
    #         content = handle.read()
    #     lines = content.strip().split("\n")
    #     print("lines", lines, list(filter(None, lines)))
    #     assert len(lines) > 0, "Empty file not allowed."

    #     def helper(args):
    #         args = args.split(" ")
    #         with tempfile.TemporaryDirectory() as tmpdirname:
    #             out = Path(tmpdirname) / f"out.{ext}"
    #             utils.python(self.layergen_exe, self.fmt.lower(), out, *args, print_output=True, cwd=tmpdirname)
    #             # TODO: log output
    #             with open(out, "rb") as handle:
    #                 raw = handle.read()
    #             return raw

    #     if len(lines) > 1:
    #         for i, args in enumerate(lines):
    #             name = f"model{i}"
    #             raw = helper(args)
    #             artifact = Artifact(f"{name}.{ext}", raw=raw, fmt=ArtifactFormat.RAW, flags=["model"])
    #         artifacts[name] = [artifact]
    #     else:
    #         artifacts["default"] = []
    #     return artifacts

    def generate(self, model) -> Tuple[dict, dict]:
        artifacts = {}
        name = model.name
        path = model.paths[0]
        ext = ModelFormats[self.fmt].extension
        with open(path, "r") as handle:
            content = handle.read()
        lines = content.strip().split("\n")
        assert len(lines) > 0, "Empty file not allowed."

        def helper(args):
            args = args.split(" ")
            with tempfile.TemporaryDirectory() as tmpdirname:
                out = Path(tmpdirname) / f"out.{ext}"
                utils.python(self.layergen_exe, self.fmt.lower(), out, *args, print_output=True, cwd=tmpdirname)
                # TODO: log output
                with open(out, "rb") as handle:
                    raw = handle.read()
                return raw

        if len(lines) > 1:
            for i, args in enumerate(lines):
                name = f"model{i}"
                raw = helper(args)
                artifact = Artifact(f"{name}.{ext}", raw=raw, fmt=ArtifactFormat.RAW, flags=["model"])
                artifacts[name] = [artifact]
            # TODO: fix this
            artifacts["default"] = artifacts["model0"]  # Dummy model because default artifacts can not be empty
        else:
            name = "default"
            raw = helper(lines[0])
            artifact = Artifact(f"{name}.{ext}", raw=raw, fmt=ArtifactFormat.RAW, flags=["model"])
            artifacts[name] = [artifact]
        return artifacts, {}
