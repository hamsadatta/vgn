import numpy as np
import pybullet as p
import skimage.transform
import time

from robot_helpers.bullet import BtCamera
from robot_helpers.spatial import Rotation, Transform
from vgn.grasp import ParallelJawGrasp


LATERAL_FFRICITON = 0.4
MAX_GRASP_FORCE = 5


class GraspSim:
    def __init__(self, cfg, rng):
        self.configure_physics_engine(cfg["gui"])
        self.configure_visualizer()
        self.sleep = cfg["sleep"]
        self.rng = rng
        self.gripper = PandaGripper(self)
        self.camera = BtCamera(320, 240, 1.047, 0.1, 2.0, renderer=p.ER_TINY_RENDERER)
        self.scene = get_scene(cfg["scene"], self)

    def configure_physics_engine(self, gui, rate=60, sub_step_count=4):
        self.rate = rate
        self.dt = 1.0 / self.rate
        p.connect(p.GUI if gui else p.DIRECT)
        p.setPhysicsEngineParameter(fixedTimeStep=self.dt, numSubSteps=sub_step_count)
        p.setGravity(0.0, 0.0, -9.81)

    def configure_visualizer(self):
        p.resetDebugVisualizerCamera(1.2, 30, -30, [0.4, 0.0, 0.2])

    def save_state(self):
        self.snapshot_id = p.saveState()

    def restore_state(self):
        p.restoreState(stateId=self.snapshot_id)

    def step(self):
        p.stepSimulation()
        if self.sleep:
            time.sleep(self.dt)


class PandaGripper:
    def __init__(self, sim):
        self.sim = sim
        self.max_width = 0.08
        self.max_depth = 0.05
        self.T_ee_com = Transform(Rotation.identity(), [0.0, 0.0, -0.025])
        self.uid = p.loadURDF("assets/panda/hand.urdf")
        self.create_joints()
        self.reset(Transform.translation(np.full(3, 100)), self.max_width)

    @property
    def width(self):
        return p.getJointState(self.uid, 0)[0] + p.getJointState(self.uid, 1)[0]

    @property
    def contacts(self):
        return p.getContactPoints(self.uid)

    def create_joints(self):
        # We replace the arm with a fixed joint (faster to simulate)
        self.fixed_joint_uid = p.createConstraint(
            parentBodyUniqueId=self.uid,
            parentLinkIndex=-1,
            childBodyUniqueId=-1,
            childLinkIndex=-1,
            jointType=p.JOINT_FIXED,
            jointAxis=[0.0, 0.0, 0.0],
            parentFramePosition=[0.0, 0.0, 0.0],
            childFramePosition=[0.0, 0.0, 0.0],
        )
        # Joint to enforce symmetric finger positions
        gear_joint_uid = p.createConstraint(
            self.uid,
            0,
            self.uid,
            1,
            p.JOINT_GEAR,
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],
        )
        p.changeConstraint(gear_joint_uid, gearRatio=-1, erp=0.1, maxForce=50)

    def reset(self, pose, width):
        pose = pose * self.T_ee_com
        p.resetBasePositionAndOrientation(
            self.uid,
            pose.translation,
            pose.rotation.as_quat(),
        )
        self.update_fixed_joint(pose)
        p.resetJointState(self.uid, 0, 0.5 * width)
        p.resetJointState(self.uid, 1, 0.5 * width)
        self.sim.step()

    def update_fixed_joint(self, target):
        p.changeConstraint(
            self.fixed_joint_uid,
            jointChildPivot=target.translation,
            jointChildFrameOrientation=target.rotation.as_quat(),
            maxForce=50,
        )

    def close_fingers(self):
        p.setJointMotorControlArray(
            self.uid,
            [0, 1],
            p.VELOCITY_CONTROL,
            targetVelocities=[-0.1] * 2,
            forces=[MAX_GRASP_FORCE] * 2,
        )
        for _ in range(60):
            self.sim.step()

    def lift(self):
        # Lift the object by 10 cm
        pos, ori = p.getBasePositionAndOrientation(self.uid)
        ori = Rotation.from_quat(ori)
        for i in range(60):
            target = pos + np.r_[0, 0, i * 1.0 / 60.0 * 0.1]
            self.update_fixed_joint(Transform(ori, target))
            self.sim.step()


def get_scene(name, sim):
    if name in scenes:
        return scenes[name](sim)
    else:
        raise ValueError("{} scene does not exist.".format(name))


def get_quality_fn(name, sim, cfg=None):
    if name in quality_fns:
        return quality_fns[name](sim, cfg)
    else:
        raise ValueError("{} quality_fn does not exist.".format(name))


class GraspQualityMetric:
    def __init__(self, sim, cfg=None):
        self.sim = sim
        self.gripper = sim.gripper
        self.rng = sim.rng


class PhysicsMetric(GraspQualityMetric):
    def __call__(self, grasp):
        return self.quality(grasp)

    def quality(self, grasp):
        self.gripper.reset(grasp.pose, grasp.width)
        if not self.gripper.contacts:
            self.gripper.close_fingers()
            self.gripper.lift()
            contacts = self.gripper.contacts
            if self.gripper.width > 0.1 * self.gripper.max_width and contacts:
                return 1.0, {"object_uid": contacts[0][2]}
        return 0.0, {}


class RobustPhysicsMetric(PhysicsMetric):
    def __init__(self, sim, cfg):
        super().__init__(sim)
        self.rng = sim.rng
        self.xyz = np.asarray(cfg["uncertainty"]["xyz"])
        self.rpy = np.asarray(cfg["uncertainty"]["rpy"])
        self.sample_count = cfg["sample_count"]

    def __call__(self, grasp):
        pose, width = grasp.pose, grasp.width
        for _ in range(self.sample_count):
            R_err = Rotation.from_euler("xyz", self.rng.uniform(-self.rpy, self.rpy))
            t_err = self.rng.uniform(-self.xyz, self.xyz)
            grasp = ParallelJawGrasp(pose * Transform(R_err, t_err), width)
            self.sim.restore_state()
            if not self.quality(grasp)[0]:
                return 0.0, {}
        return 1.0, {}


def load_urdf(urdf, pose, scaling=1.0):
    ori, pos = pose.rotation.as_quat(), pose.translation
    return p.loadURDF(str(urdf), pos, ori, globalScaling=scaling)


class Scene:
    def __init__(self, sim):
        self.support_urdf = "assets/plane/model.urdf"
        self.sim = sim
        self.rng = sim.rng
        self.size = 0.3
        self.origin = None
        self.center = None
        self.support_uid = -1
        self.object_uids = []

    @property
    def object_count(self):
        return len(self.object_uids)

    def clear(self):
        self.remove_support()
        self.remove_all_objects()

    def generate(self):
        raise NotImplementedError

    def add_support(self, pose):
        self.support_uid = load_urdf(self.support_urdf, pose, 0.3)

    def add_object(self, urdf, pose, scaling):
        uid = load_urdf(urdf, pose, scaling)
        self.object_uids.append(uid)
        p.changeDynamics(uid, -1, lateralFriction=LATERAL_FFRICITON)
        return uid

    def remove_support(self):
        p.removeBody(self.support_uid)

    def remove_object(self, uid):
        p.removeBody(uid)
        self.object_uids.remove(uid)

    def remove_all_objects(self):
        for uid in list(self.object_uids):
            self.remove_object(uid)

    def remove_outside_objects(self):
        # TODO does not handle rotations
        self.wait_for_objects_to_rest()
        for uid in self.object_uids:
            xyz = np.asarray(p.getBasePositionAndOrientation(uid)[0])
            xyz = xyz - self.origin.translation
            if np.any(xyz < 0.0) or np.any(xyz > self.size):
                self.remove_object(uid)

    def wait_for_objects_to_rest(self):
        for _ in range(60):  # TODO
            self.sim.step()


class PackedScene(Scene):
    def generate(self, origin, urdfs, scalings=1.0, max_attempts=10):
        if isinstance(scalings, float):
            scalings = [scalings] * len(urdfs)
        self.origin = origin
        self.center = origin * Transform.t([0.5 * self.size, 0.5 * self.size, 0])
        self.add_support(self.center)
        for urdf, scaling in zip(urdfs, scalings):
            uid = self.add_object(urdf, Transform.identity(), scaling)
            lower, upper = p.getAABB(uid)
            z_offset = 0.5 * (upper[2] - lower[2]) + 0.002
            state_id = p.saveState()
            for _ in range(max_attempts):
                local_ori = Rotation.from_rotvec([0, 0, self.rng.uniform(0, 2 * np.pi)])
                local_pos = np.r_[self.rng.uniform(0.2, 0.8, 2) * self.size, z_offset]
                pose = origin * Transform(local_ori, local_pos)
                p.resetBasePositionAndOrientation(
                    uid,
                    pose.translation,
                    pose.rotation.as_quat(),
                )
                self.sim.step()
                if p.getContactPoints(uid):
                    p.restoreState(stateId=state_id)
                else:
                    break
            else:
                self.remove_object(uid)
        self.remove_outside_objects()


class PileScene(Scene):
    def generate(self, origin, urdfs, scalings=1.0):
        if isinstance(scalings, float):
            scalings = [scalings] * len(urdfs)
        self.origin = origin
        self.center = origin * Transform.t([0.5 * self.size, 0.5 * self.size, 0])
        self.add_support(self.center)
        uid = load_urdf("assets/box/model.urdf", Transform.t([0.02, 0.02, 0.05]), 1.3)
        for urdf, scaling in zip(urdfs, scalings):
            loc_ori = Rotation.random(random_state=self.rng)
            loc_pos = np.r_[self.rng.uniform(self.size / 3, 2 * self.size / 3, 2), 0.25]
            pose = origin * Transform(loc_ori, loc_pos)
            self.add_object(urdf, pose, scaling)
            self.wait_for_objects_to_rest()
        p.removeBody(uid)
        self.remove_outside_objects()


scenes = {"packed": PackedScene, "pile": PileScene}
quality_fns = {"physics": PhysicsMetric, "robust_physics": RobustPhysicsMetric}


def apply_noise(img, k=1000, theta=0.001, sigma=0.005, l=4.0):
    # Multiplicative and additive noise
    img *= np.random.gamma(k, theta)
    h, w = img.shape
    noise = np.random.randn(int(h / l), int(w / l)) * sigma
    img += skimage.transform.resize(noise, img.shape, order=1, mode="constant")
    return img
