import collections
import copy
import math
import os

import matplotlib.pyplot as plt
import numpy as np

import habitat_sim
import habitat_sim.bindings as hsim
from habitat_sim.agent import AgentState
from habitat_sim.utils.common import quat_from_two_vectors
from habitat_sim.utils.filesystem import search_dir_tree_for_ext
from habitat_sim.utils.data.poseextractor import PoseExtractor


class ImageExtractor:
    r"""Main class that extracts data by creating a simulator and generating a topdown map from which to
    iteratively generate image data.

    :property scene_filepath: The location of the .glb file given to the simulator
    :property labels: class labels of things to tather images of
    :property cfg: configuration for simulator of type SimulatorConfiguration
    :property sim: Simulator object
    :property pixels_per_meter: Resolution of topdown map. 0.1 means each pixel in the topdown map
        represents 0.1 x 0.1 meters in the coordinate system of the pathfinder
    :property tdv: TopdownView object
    :property topdown_view: The actual 2D array representing the topdown view
    :property pose_extractor: PoseExtractor object
    :property poses: list of camera poses gathered from pose_extractor
    :property label_map: maps lable numbers on the topdown map to their name
    :property out_name_to_sensor_name: maps name of output to the sensor same corresponding to that output
    :property output: list of output names that the user wants e.g. ['rgba', 'depth']
    """

    def __init__(
        self,
        filepath,
        labels=[0.0],
        img_size=(512, 512),
        output=["rgba"],
        sim=None,
        shuffle=True,
        split=(70, 30),
    ):
        if sum(split) != 100:
            raise Exception("Train/test split must sum to 100.")

        self.scene_filepaths = None
        self.cur_fp = None
        if os.path.isdir(filepath):
            self.scene_filepaths = search_dir_tree_for_ext(filepath, ".glb")
        else:
            self.scene_filepaths = [filepath]
            self.cur_fp = filepath

        self.labels = set(labels)
        self.img_size = img_size
        self.cfg = make_config_default_settings(self.scene_filepaths[0], self.img_size)

        if sim is None:
            sim = habitat_sim.Simulator(self.cfg)
        else:
            # If a sim is provided we have to make a new cfg
            self.cfg = config_sim(sim.config.sim_cfg.scene.id, img_size)
            sim.reconfigure(self.cfg)

        self.sim = sim
        self.pixels_per_meter = 0.1
        self.tdv_fp_ref_triples = self.precomute_tdv_and_refs(
            self.sim, self.scene_filepaths, self.res
        )

        # self.tdv = TopdownView(self.sim, self.res)
        # self.topdown_view = self.tdv.topdown_view
        self.pose_extractor = PoseExtractor(
            self.tdv_fp_ref_triples, self.sim, self.pixels_per_meter
        )
        self.poses = self.pose_extractor.extract_poses(
            labels=self.labels
        )  # list of poses

        if shuffle:
            np.random.shuffle(self.poses)

        self.train, self.test = self._handle_split(split, self.poses)
        self.mode = "full"
        self.mode_to_data = {
            "full": self.poses,
            "train": self.train,
            "test": self.test,
            None: self.poses,
        }

        self.instance_id_to_name = self._generate_label_map(self.sim.semantic_scene)
        self.out_name_to_sensor_name = {
            "rgba": "color_sensor",
            "depth": "depth_sensor",
            "semantic": "semantic_sensor",
        }
        self.output = output

    def __len__(self):
        return len(self.mode_to_data[self.mode])

    def __getitem__(self, idx):
        if isinstance(idx, slice):
            start, stop, step = idx.start, idx.stop, idx.step
            if start is None:
                start = 0
            if stop is None:
                stop = len(self.mode_to_data[self.mode])
            if step is None:
                step = 1

            return [
                self.__getitem__(i)
                for i in range(start, stop, step)
                if i < len(self.mode_to_data[self.mode])
            ]

        mymode = self.mode.lower()
        poses = self.mode_to_data[mymode]
        pos, rot, label, fp = poses[idx]

        # Only switch scene if it is different from the last one accessed
        if fp != self.cur_fp:
            self.sim.reconfigure(make_config_default_settings(fp, self.img_size))
            self.cur_fp = fp

        new_state = AgentState()
        new_state.position = pos
        new_state.rotation = rot
        self.sim.agents[0].set_state(new_state)
        obs = self.sim.get_sensor_observations()
        sample = {
            out_name: obs[self.out_name_to_sensor_name[out_name]]
            for out_name in self.output
        }
        #sample["label"] = self.label_map[label]

        return sample

    def precomute_tdv_and_refs(self, sim, scene_filepaths, res):
        tdv_fp_ref = []
        for filepath in scene_filepaths:
            cfg = make_config_default_settings(filepath, self.img_size)
            sim.reconfigure(cfg)
            ref_point = self._get_pathfinder_reference_point(sim.pathfinder)
            tdv = TopdownView(sim, ref_point[1], res=res)
            tdv_fp_ref.append((tdv, filepath, ref_point))
        
        return tdv_fp_ref

    def close(self):
        r"""Deletes the instance of the simulator. Necessary for instatiating a different ImageExtractor.
        """
        self.sim.close()
        del self.sim

    def set_mode(self, mode):
        mymode = mode.lower()
        if mymode not in ["full", "train", "test"]:
            raise Exception(
                f'Mode {mode} is not a valid mode for ImageExtractor. Please enter "full, train, or test"'
            )

        self.mode = mymode

    def get_semantic_class_names(self):
        class_names = list(set(
            name for name in self.instance_id_to_name.values() if name != 'background'
        ))
        class_names = ['background'] + class_names # Make sure background is index 0
        return class_names

    def _handle_split(self, split, poses):
        train, test = split
        num_poses = len(self.poses)
        last_train_idx = int((train / 100) * num_poses)
        train_poses = poses[:last_train_idx]
        test_poses = poses[last_train_idx:]
        return train_poses, test_poses

    def _get_pathfinder_reference_point(self, pf):
        bound1, bound2 = pf.get_bounds()
        startw = min(bound1[0], bound2[0])
        starth = min(bound1[2], bound2[2])
        starty = pf.get_random_navigable_point()[
            1
        ]  # Can't think of a better way to get a valid y-axis value
        return (startw, starty, starth)  # width, y, height

    def _generate_label_map(self, scene, verbose=False):
        if verbose:
            print(f"House has {len(scene.levels)} levels, {len(scene.regions)} regions and {len(scene.objects)} objects")
            print(f"House center:{scene.aabb.center} dims:{scene.aabb.sizes}")

        instance_id_to_name = {}
        for obj in scene.objects:
            if obj and obj.category:
                obj_id = int(obj.id.split('_')[-1])
                instance_id_to_name[obj_id] = obj.category.name()
    
        return instance_id_to_name
    
    def _config_sim(self, scene_filepath, img_size):
        settings = {
            "width": img_size[1],  # Spatial resolution of the observations
            "height": img_size[0],
            "scene": scene_filepath,  # Scene path
            "default_agent": 0,
            "sensor_height": 1.5,  # Height of sensors in meters
            "color_sensor": True,  # RGBA sensor
            "semantic_sensor": True,  # Semantic sensor
            "depth_sensor": True,  # Depth sensor
            "silent": True,
        }

        sim_cfg = hsim.SimulatorConfiguration()
        sim_cfg.enable_physics = False
        sim_cfg.gpu_device_id = 0
        sim_cfg.scene.id = settings["scene"]

        # define default sensor parameters (see src/esp/Sensor/Sensor.h)
        sensors = {
            "color_sensor": {  # active if sim_settings["color_sensor"]
                "sensor_type": hsim.SensorType.COLOR,
                "resolution": [settings["height"], settings["width"]],
                "position": [0.0, settings["sensor_height"], 0.0],
            },
            "depth_sensor": {  # active if sim_settings["depth_sensor"]
                "sensor_type": hsim.SensorType.DEPTH,
                "resolution": [settings["height"], settings["width"]],
                "position": [0.0, settings["sensor_height"], 0.0],
            },
            "semantic_sensor": {  # active if sim_settings["semantic_sensor"]
                "sensor_type": hsim.SensorType.SEMANTIC,
                "resolution": [settings["height"], settings["width"]],
                "position": [0.0, settings["sensor_height"], 0.0],
            },
        }

        # create sensor specifications
        sensor_specs = []
        for sensor_uuid, sensor_params in sensors.items():
            if settings[sensor_uuid]:
                sensor_spec = hsim.SensorSpec()
                sensor_spec.uuid = sensor_uuid
                sensor_spec.sensor_type = sensor_params["sensor_type"]
                sensor_spec.resolution = sensor_params["resolution"]
                sensor_spec.position = sensor_params["position"]
                sensor_spec.gpu2gpu_transfer = False
                sensor_specs.append(sensor_spec)

        # create agent specifications
        agent_cfg = habitat_sim.agent.AgentConfiguration()
        agent_cfg.sensor_specifications = sensor_specs

        return habitat_sim.Configuration(sim_cfg, [agent_cfg])

    def _get_pathfinder_reference_point(self, pf):
        bound1, bound2 = pf.get_bounds()
        startw = min(bound1[0], bound2[0])
        starth = min(bound1[2], bound2[2])
        starty = pf.get_random_navigable_point()[
            1
        ]  # Can't think of a better way to get a valid y-axis value
        return (startw, starty, starth)  # width, y, height


class TopdownView(object):
    def __init__(self, sim, height, pixels_per_meter=0.1):
        self.topdown_view = np.array(
            sim.pathfinder.get_topdown_view(pixels_per_meter, height)
        ).astype(np.float64)