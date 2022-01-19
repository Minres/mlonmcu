import os
import argparse
import logging
from pathlib import Path
from abc import ABC, abstractmethod

from mlonmcu.cli.helper.parse import extract_feature_names, extract_config
from mlonmcu.feature.feature import FeatureType
from mlonmcu.logging import get_logger
from mlonmcu.artifact import Artifact

logger = get_logger()


class Backend(ABC):

    shortname = None

    FEATURES = []
    DEFAULTS = {}
    REQUIRED = []

    def __init__(
        self,
        framework="",
        features=None,
        config=None,
        context=None,
    ):
        self.framework = framework
        self.features = features if features else []
        self.config = config if config else {}
        self.process_features()
        self.filter_config()
        self.context = context
        self.artifacts = []

    def __repr__(self):
        name = type(self).shortname
        return f"Backend({name})"

    def process_features(self):
        # Filter out non-backend features
        self.features = [
            feature
            for feature in self.features
            if FeatureType.BACKEND in feature.types()
        ]
        for feature in self.features:
            feature.add_backend_config(self.name, self.config)

    def remove_config_prefix(self, config):
        def helper(key):
            return key.split(f"{self.name}.")[-1]

        return {
            helper(key): value
            for key, value in config.items()
            if f"{self.name}." in key
        }

    def filter_config(self):
        cfg = self.remove_config_prefix(self.config)
        for required in self.REQUIRED:
            value = None
            if required in cfg:
                value = cfg[required]
            elif required in self.config:
                value = self.config[required]
            assert value is not None, f"Required config key can not be None: {required}"

        for key in self.DEFAULTS:
            if key not in cfg:
                cfg[key] = self.DEFAULTS[key]

        for key in cfg:
            if key not in list(self.DEFAULTS.keys()) + self.REQUIRED:
                logger.warn("Backend received an unknown config key: %s", key)
                del cfg[key]

        self.config = cfg

    @abstractmethod
    def load_model(self, model):
        pass

    @abstractmethod
    def generate_code(self):
        pass

    def export_code(self, path):
        assert (
            len(self.artifacts) > 0
        ), "No artifacts found, please run generate_code() first"

        if not isinstance(path, Path):
            path = Path(path)

        is_dir = len(path.suffix) == 0
        if is_dir:
            assert (
                path.is_dir()
            ), "The supplied path does not exists."  # Make sure it actually exists (we do not create it by default)
            for artifact in self.artifacts:
                # TODO: move the following to a helper function and share code
                dest = path / artifact.name
                with open(dest, "w") as outfile:
                    logger.info(f"Exporting artifact: {artifact.name}")
                    outfile.write(artifact.content)
        else:
            assert (
                path.parent.is_dir()
            ), "The parent directory does not exist. Make sure to create if beforehand."
            # Warning: the first artifact is considered as main
            # We need to ensure that all further artifacts share the same prefix
            main_prefix = None
            for artifact in self.artifacts:
                if not main:
                    main_prefix = artifact.name
                else:
                    if main_prefix not in artifact.name:
                        logger.warn(
                            f"Skipping export of artifact '{artifact.name}' to prevent overwriting random files"
                        )
                        continue
                dest = path / artifact.name
                with open(dest, "w") as outfile:
                    logger.info(f"Exporting artifact: {artifact.name}")
                    outfile.write(artifact.content)

    def get_cmake_args(self):
        assert self.shortname is not None
        return [f"-DBACKEND={self.shortname}"]

    def add_cmake_args(self, args):
        args += self.get_cmake_args()


def get_parser(backend_name, features, defaults):
    # TODO: add help strings should start with a lower case letter
    parser = argparse.ArgumentParser(
        description=f"Run {backend_name} backend",
        formatter_class=argparse.RawTextHelpFormatter,
        # epilog="""Use environment variables to overwrite default paths:""",
    )
    parser.add_argument(
        "model", metavar="MODEL", type=str, nargs=1, help="Model to process"
    )
    parser.add_argument(
        "--output",
        "-o",
        metavar="DIR",
        type=str,
        default=os.path.join(os.getcwd(), "out"),  # TODO: keep this or require flag?
        help="""Output directory/file (default: %(default)s)""",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed messages for easier debugging (default: %(default)s)",
    )
    parser.add_argument(
        "--print",
        "-p",
        action="store_true",
        help="Print the generated code to the command line instead (default: %(default)s)",  # TODO: instead or both?
    )
    parser.add_argument(
        "-f",
        "--feature",
        type=str,
        metavar="FEATURE",
        # nargs=1,
        action="append",
        choices=list(dict.fromkeys(features)),
        help="Enabled features for the backend (default: %(default)s choices: %(choices)s)",
    )
    parser.add_argument(
        "-c",
        "--config",
        metavar="KEY=VALUE",
        nargs="+",
        action="append",
        help=f"""Set a number of key-value pairs

Allowed options:
"""
        + "\n".join(
            [
                f"- [{backend_name}].{key} (Default: {value})"
                for key, value in defaults.items()
            ]
        ),
    )
    return parser


def init_backend_features(names, config):
    features = []
    for name in names:
        feature_classes = get_supported_features(
            feature_type=FeatureType.BACKEND, feature_name=name
        )
        for feature_class in feature_classes:
            features.append(feature_class(config=config))
    return features


def main(backend_name, backend, backend_features, backend_defaults, args=None):
    parser = get_parser(
        backend_name, features=backend_features, defaults=backend_defaults
    )
    if args:
        args = parser.parse_args(args)
    else:
        args = parser.parse_args()
    # TODO: handle args!
    model = Path(args.model[0])
    config = extract_config(args)
    features_names = extract_feature_names(args)
    features = init_backend_features(features_names, config)
    backend_inst = backend(features=features, config=config)
    backend_inst.load_model(model)
    backend_inst.generate_code()
    if args.print:
        print("Printing generated artifacts:")
        for artifact in backend_inst.artifacts:
            print(f"=== {artifact.name} ===")
            artifact.print_summary()
            print("=== End ===")
            print()
    else:
        out = args.output
        backend_inst.export_code(out)
