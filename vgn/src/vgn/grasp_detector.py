import logging


from mayavi import mlab
import numpy as np
from scipy import ndimage
import torch

from vgn.grasp import Grasp
from vgn.utils.transform import Transform, Rotation
from vgn.networks import get_network
from vgn.utils.vis import draw_volume, show_sample


class GraspDetector(object):
    def __init__(
        self,
        device,
        network_path,
        threshold=0.9,
        show_out_vol=False,
        show_filtered_vol=False,
        show_detections=False,
    ):
        self.device = device
        self.net = get_network(network_path.name.split("_")[1]).to(self.device)
        self.net.load_state_dict(torch.load(network_path, map_location=self.device))

        self.threshold = threshold
        self.show_out_vol = show_out_vol
        self.show_filtered_vol = show_filtered_vol
        self.show_detections = show_detections

    def detect_grasps(self, tsdf):
        """Detect grasps in the given volume.
        
        Args:
            tsdf (np.ndarray): A 1x40x40x40 voxel grid with truncated signed distances.

        Returns:
            List of grasp candidates in voxel coordinates and their associated predicted qualities.
        """

        qual, rot, width = self._predict(tsdf)

        mask = self._filter_grasps(tsdf, qual, rot, width)
        grasps, qualities = self._select_grasps(qual, rot, width, mask)
        grasps, qualities = self._sort_grasps(grasps, qualities)

        if self.show_detections:
            show_sample(tsdf, qual, rot, width, mask)

        return grasps, qualities

    def _predict(self, tsdf):
        tsdf = torch.from_numpy(tsdf).unsqueeze(0).to(self.device)

        with torch.no_grad():
            qual, rot, width = self.net(tsdf)

        qual = qual.cpu().squeeze().numpy()
        rot = rot.cpu().squeeze().numpy()
        width = width.cpu().squeeze().numpy() * 10

        if self.show_out_vol:
            mlab.figure()
            draw_volume(qual)

        return qual, rot, width

    def _filter_grasps(self, tsdf, qual, rot, width):
        qual = qual.copy()

        qual[tsdf.squeeze() == 0.0] = 0.0
        qual[qual < self.threshold] = 0.0

        max_vol = ndimage.maximum_filter(qual, size=5)
        qual = np.where(qual == max_vol, qual, 0.0)
        mask = np.where(qual, 1.0, 0.0)

        if self.show_filtered_vol:
            mlab.figure()
            draw_volume(qual)

        return mask

    def _select_grasps(self, qual_vol, rot_vol, width_vol, mask):
        grasps, qualities = [], []

        for index in np.argwhere(mask):
            i, j, k = index

            qual = qual_vol[i, j, k]
            ori = Rotation.from_quat(rot_vol[:, i, j, k])
            pos = np.r_[i, j, k]
            width = width_vol[i, j, k]

            grasps.append(Grasp(Transform(ori, pos), width))
            qualities.append(qual)

        return np.asarray(grasps), np.asarray(qualities)

    def _sort_grasps(self, grasps, qualities):
        indices = np.argsort(-qualities)
        grasps = grasps[indices]
        qualities = qualities[indices]
        return grasps, qualities
