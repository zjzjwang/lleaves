import time
from statistics import mean, pstdev

import lightgbm
import matplotlib.pyplot as plt
import numpy as np
import onnxmltools
import onnxruntime as rt
import pandas as pd
import seaborn
import treelite
import treelite_runtime
from onnxconverter_common import FloatTensorType
from train_NYC_model import feature_enginering

from lleaves import Model


class BenchmarkModel:
    model = None
    name = None

    def __init__(self, lgbm_model_file):
        self.model_file = lgbm_model_file

    def setup(self, data, n_threads):
        raise NotImplementedError()

    def predict(self, data, index, batchsize, n_threads):
        self.model.predict(data[index : index + batchsize])

    def __str__(self):
        return self.name


class LGBMModel(BenchmarkModel):
    name = "LightGBM Booster"

    def setup(self, data, n_threads):
        self.model = lightgbm.Booster(model_file=self.model_file)

    def predict(self, data, index, batchsize, n_threads):
        self.model.predict(
            data[index : index + batchsize], n_jobs=n_threads if n_threads else None
        )


class LLVMModel(BenchmarkModel):
    name = "LLeaVes"

    def setup(self, data, n_threads):
        self.model = Model(model_file=self.model_file)
        self.model.compile()


class TreeliteModel(BenchmarkModel):
    name = "Treelite"

    def setup(self, data, n_threads):
        treelite_model = treelite.Model.load(self.model_file, model_format="lightgbm")
        treelite_model.export_lib(toolchain="gcc", libpath="/tmp/treelite_model.so")
        self.model = treelite_runtime.Predictor(
            "/tmp/treelite_model.so",
            nthread=n_threads if n_threads != 0 else None,
        )

    def predict(self, data, index, batchsize, n_threads):
        return self.model.predict(treelite_runtime.DMatrix(data[i : i + batchsize]))


class TreeliteModelAnnotatedBranches(TreeliteModel):
    name = "Treelite (Annotated Branches)"

    def setup(self, data, n_threads):
        treelite_model = treelite.Model.load(self.model_file, model_format="lightgbm")
        annotator = treelite.Annotator()
        annotator.annotate_branch(
            model=treelite_model, dmat=treelite_runtime.DMatrix(data)
        )
        annotator.save(path="/tmp/model-annotation.json")
        treelite_model.export_lib(
            toolchain="gcc",
            libpath="/tmp/treelite_model_with_branches.so",
            params={"annotate_in": "/tmp/model-annotation.json"},
        )
        self.model = treelite_runtime.Predictor(
            "/tmp/treelite_model_with_branches.so",
            nthread=n_threads if n_threads != 0 else None,
        )


class ONNXModel(BenchmarkModel):
    name = "ONNX"

    def setup(self, data, n_threads):
        lgbm_model = lightgbm.Booster(model_file=self.model_file)
        onnx_model = onnxmltools.convert_lightgbm(
            lgbm_model,
            initial_types=[
                (
                    "float_input",
                    FloatTensorType([None, lgbm_model.num_feature()]),
                )
            ],
            target_opset=8,
        )
        onnxmltools.utils.save_model(onnx_model, "/tmp/model.onnx")
        options = rt.SessionOptions()
        options.inter_op_num_threads = n_threads
        options.intra_op_num_threads = n_threads
        self.model = rt.InferenceSession("/tmp/model.onnx", sess_options=options)
        self.input_name = self.model.get_inputs()[0].name
        self.label_name = self.model.get_outputs()[0].name

    def predict(self, data, index, batchsize, n_threads):
        return self.model.run(
            [self.label_name], {self.input_name: data[index : index + batchsize]}
        )


def save_plots(results_full, title, n_threads, batchsizes):
    fig, axs = plt.subplots(ncols=2, nrows=1, sharey="row")
    fig.suptitle(title, fontsize=16)
    fig.set_size_inches(18.5, 10.5)
    keys = sorted(results_full.keys())
    for count, n_thread in enumerate(n_threads):
        for key in keys:
            if key.startswith(str(n_thread)):
                seaborn.lineplot(
                    x="batchsize",
                    y="time (μs)",
                    ci="sd",
                    data=results_full[key],
                    ax=axs[count],
                    label=key.split("_")[1] if count == 1 else None,
                )
        axs[count].set(
            xscale="log",
            title=f"n_threads={str(n_thread).replace('0', 'not limited')}",
            xticks=batchsizes,
            xticklabels=batchsizes,
            xlim=(1, None),
        )
    plt.yscale("log")
    plt.savefig(f"{title}.png")


if __name__ == "__main__":
    used_columns = [
        "fare_amount",
        "pickup_latitude",
        "pickup_longitude",
        "dropoff_latitude",
        "dropoff_longitude",
        "tpep_pickup_datetime",
        "passenger_count",
    ]
    df = pd.read_parquet("data/yellow_tripdata_2016-01.parquet", columns=used_columns)
    NYC_X = feature_enginering().fit_transform(df).astype(np.float32)

    df = pd.read_csv("data/airline_data_factorized.csv")
    airline_X = df.to_numpy(np.float32)

    model_file_NYC = "../tests/models/NYC_taxi/model.txt"
    model_file_airline = "../tests/models/airline/model.txt"

    batchsizes = [1, 2, 3, 5, 7, 10, 30, 70, 100, 200, 300]
    model_classes = [
        LGBMModel,
        TreeliteModel,
        ONNXModel,
        LLVMModel,
    ]
    n_threads = [0, 1]
    for model_file, data in zip(
        [model_file_airline, model_file_NYC],
        [airline_X, NYC_X],
    ):
        print(model_file, "\n")
        results_full = {}
        for n_thread in n_threads:
            for model_class in model_classes:
                if model_file == model_file_airline and model_class == ONNXModel:
                    # ONNX doesn't like the categorical model, don't know why
                    continue

                model = model_class(model_file)
                results = {"time (μs)": [], "batchsize": []}
                results_full[f"{n_thread}_{model}"] = results
                model.setup(data, n_thread)
                for batchsize in batchsizes:
                    times = []
                    for _ in range(100):
                        start = time.perf_counter_ns()
                        for _ in range(30):
                            for i in range(50):
                                model.predict(data, i, batchsize, n_thread)
                        # calc per-batch times, in μs
                        times.append(
                            (time.perf_counter_ns() - start) / (30 * 50) / 1000
                        )
                    results["time (μs)"] += times
                    results["batchsize"] += len(times) * [batchsize]
                    print(
                        f"{model} (Batchsize {batchsize}): {round(mean(times), 2)}μs ± {round(pstdev(times), 2)}μs"
                    )
        save_plots(results_full, model_file.split("/")[-2], n_threads, batchsizes)
