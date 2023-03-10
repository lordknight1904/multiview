import os
import torch
import torch.nn.functional as F
from dataclasses import dataclass, field
import numpy as np
from PIL import Image
from typing import Tuple, List, ClassVar
from scipy.spatial.transform import Rotation as R  # rotation axis ??? left-hand / clockwise
# from transformers import AutoImageProcessor, DetaForObjectDetection
from mmdet.apis import init_detector, inference_detector, show_result_pyplot

from matplotlib import pyplot as plt
import matplotlib.patches as patches
import matplotlib.image as mpimg


@dataclass
class Line:
    origin: np.ndarray
    direction: np.ndarray

    @classmethod
    def from_camera_coor(cls, origin, coor):
        return cls(np.array(origin), np.array(coor))
    
    def point(self, r):
        return self.origin + r*self.direction
    

@dataclass
class Camera:
    filename: str=''
    position: np.ndarray=np.array([0., 0., 0.])
    quaternion: np.ndarray=np.array([1., 0., 0., 0.])  # angle in quaternion

    resolution: np.ndarray=np.array([3840, 2160])
    sensor_size: np.ndarray=np.array([30, 30])
    matrix: np.ndarray=np.array([0, 0, 0, 0, 0, 0, 0, 0, 0])
    # intrinsic: np.ndarray=np.array([[0, 0, 0], [0, 0, 0], [0, 0, 0]])
    
    @staticmethod
    def qm_2(quaternion, vector):
        q0, q1, q2, q3 = quaternion
        matrix = np.array([
            [1-2*q2**2-2*q3**2, 2*(q1*q2+q0*q3), 2*(q1*q3-q0*q2)],
            [2*(q1*q2-q0*q3), 1-2*q1**2-2*q3**2, 2*(q2*q3+q0*q1)],
            [2*(q1*q3+q0*q2), 2*(q2*q3-q0*q1), 1-2*q1**2-2*q2**2]
        ])

        return np.matmul(vector, matrix.T)

    @staticmethod
    def _convert_to_ndarray(obj):
        if not isinstance(obj, (np.ndarray, np.generic)):
            return np.array(obj)
        return obj

    def __post_init__(self):
        self.position = self._convert_to_ndarray(self.position)
        self.quaternion = self._convert_to_ndarray(self.quaternion)
        self.resolution = self._convert_to_ndarray(self.resolution)
        self.sensor_size = self._convert_to_ndarray(self.sensor_size)  # measured in millimeters
        self.matrix = self._convert_to_ndarray(self.matrix).reshape((3, 3))

        focal = 20.78461  # measured in millimeters
        self.intrinsic = np.array([
            [focal*self.resolution[0]/self.sensor_size[0], 0, 0],
            [0, -focal*self.resolution[0]/self.sensor_size[1], 0],
            [self.resolution[0]/2, self.resolution[1]/2, 1],
        ])
    
    def world_to_camera(self, world_coor: List[np.ndarray]):
        world_coor = [coor - self.position for coor in world_coor]  # translation
        # rotation
        camera_coor = [
            self.qm_2(self.quaternion, coor)
            for coor in world_coor
        ]
        return camera_coor

    def camera_to_pixel(self, camera_coor: List[np.ndarray]) -> List[np.ndarray]:
        ndc_coor = [np.matmul(coor, self.intrinsic) for coor in camera_coor]
        pixel_coor = [np.floor((coor/coor[-1])[:-1]) for coor in ndc_coor]

        return pixel_coor

    def pixel_to_ray(self, pixel_coor):

        inv_intrinsic = np.linalg.inv(self.intrinsic)
        ndc_coor = [
            np.matmul(np.array([*coor, 1]), inv_intrinsic)
            for coor in pixel_coor
        ]

        q_ = self.quaternion
        camera_coor = [
            self.qm_2(np.array([q_[0], -q_[1], -q_[2], -q_[3]]), coor)
            for coor in ndc_coor
        ]

        return [Line.from_camera_coor(self.position, coor) for coor in camera_coor]
    
    @classmethod
    def from_capture(cls, f_dir, capture):
        q_ = capture['rotation']
        return cls(
            filename=os.path.join(f_dir, capture['filename']),
            position=capture['position'],
            quaternion=np.array([q_[3], q_[0], q_[1], q_[2]]),
            resolution=capture['dimension'],
            matrix=capture['matrix'],
        )


@dataclass
class BoundingBox:
    instanceId: str
    center: np.ndarray
    size: np.ndarray
    
    @classmethod
    def from_anno(cls, info):
        size = np.array([dim/2 for dim in info['dimension']])
        return cls(
            instanceId=info['instanceId'],
            center=np.array(info['origin']) + size,
            size=size
        )


@dataclass
class Location:
    origin: np.ndarray

    @classmethod
    def from_info(cls, info, rotation, offset):
        ret = Camera.qm_2(
            rotation*np.array([1, -1, -1, -1]),
            np.array(info['translation'])
        )
        return cls(ret + offset)
    

@dataclass
class DeepCamera(Camera):
    object_detector: torch.nn.Module=None
    resnet: torch.nn.Module=None
    preprocess: torch.nn.Module=None

    gt_bboxes: List[BoundingBox]=field(default_factory=list)
    location: Location=None

    pred_bboxes: List[BoundingBox]=field(init=False)

    def __post_init__(self):
        super().__post_init__()
        result = inference_detector(self.object_detector, self.filename)
        
        for i in range(len(result[1])):
            if len(result[1][i]) > 0:
                for j in range(len(result[1][i])):
                    result[1][i][j] = np.zeros_like(result[1][i][j], dtype=bool)
        
        image = Image.open(self.filename).convert('RGB')
        human_pred = result[0][0]
        
        self.preds = []
        # for pred in human_pred:
        for pred in human_pred:
            if pred[-1] < 0.9:
                continue
            bbox = BoundingBox(
                instanceId=-1,
                center=np.array([(pred[0] + pred[2])/2, (pred[1] + pred[3])/2]),
                size=np.array([(pred[2]-pred[0])/2, (pred[3]-pred[1])/2])
            )
            tl, br = bbox.center - bbox.size, bbox.center + bbox.size
            image_ = image.crop((*tl, *br))
            image_tensor = torch.tensor(np.array(image_), dtype=torch.float32).permute((2, 0, 1)).unsqueeze(0)
            
            image_tensor = self.preprocess(image_tensor)
            with torch.no_grad():
                box_feature = self.resnet(image_tensor.cuda())
                print(box_feature.shape)
            # print(box_feature[0][:20])

            t, l = tl
            b, r = br
            max_iou = 0
            for _, gt_box in self.gt_bboxes:
                tl_, br_ = gt_box.center - gt_box.size, gt_box.center + gt_box.size
                t_, l_ = tl_
                b_, r_ = br_

                o = np.array([max(t, t_), max(l, l_)])
                u = np.array([min(b, b_), min(r, r_)])

                w = u[0] - o[0]
                h = u[1] - o[1]
                intersection = w * h
                union = ((b-t)*(r-l)) + ((b_-t_)*(r_-l_)) - intersection
                iou = intersection / (union + 1e-8)
                max_iou = max(max_iou, iou)

            self.preds.append((
                bbox,
                pred[-1],
                max_iou,
                box_feature[0].cpu(),
                self.pixel_to_ray([np.array([(pred[0] + pred[2])/2, (pred[1] + pred[3])/2])])[0]
                # self.pixel_to_ray([bbox.center])[0]
            ))
            # print('a'*30)
        
        self.gt_lines = [(box.instanceId, self.pixel_to_ray([box.center])[0]) for box in self.gt_bboxes]
            
    @classmethod
    def from_capture(cls, f_dir, capture, object_detector, resnet, preprocess):
        camera = Camera.from_capture(f_dir, capture)
        annotations = [anno['values'] for anno in capture['annotations'] if '2D' in anno['id']][0]
        return cls(
            filename=camera.filename,
            position=camera.position,
            quaternion=camera.quaternion,
            resolution=camera.resolution,
            sensor_size=camera.sensor_size,
            matrix=camera.matrix,
            object_detector=object_detector,
            resnet=resnet, preprocess=preprocess,
            gt_bboxes=[BoundingBox.from_anno(anno) for anno in annotations]
        )
    

class Scene:
    offset = np.array([0.00956252, 0, -0.068264])

    def __init__(self, f_dir, cameras: List[DeepCamera], locations: List[Location]):
        self.f_dir = f_dir
        self.cameras = cameras
        self.locations = locations
        # self.object_pairs = self._object_pairing()

        self.gt_locations = self._get_locations()

    @staticmethod
    def _find_points(line_a: Line, line_b: Line):
        d = line_b.origin - line_a.origin
        u = np.dot(line_a.direction, line_b.direction)
        e = np.dot(line_a.direction, d)
        f = np.dot(line_b.direction, d)

        r_1 = (e - u*f) / (1 - u**2)
        r_2 = (f - u*e) / (u**2 - 1)

        p1 = line_a.point(r_1)
        p2 = line_b.point(r_2)

        return (p1 + p2) / 2
    
    def _get_locations(self):
        camera_0 = self.cameras[0]
        camera_1 = self.cameras[1]

        locations = []
        for line_a in camera_0.gt_lines:
            for line_b in camera_1.gt_lines:
                if line_a[0] == line_b[0]:  # same instance_id
                    locations.append((line_a[0], self._find_points(line_a[1], line_b[1])))
                    break
        return locations

    def _object_pairing(self):
        camera_0 = self.cameras[0].preds
        camera_1 = self.cameras[1].preds

        features_1 = torch.stack([pred[3] for pred in camera_1])

        for i, pred in enumerate(camera_0):
            feature = pred[3]
            # print(feature[:10])
            similarity = F.cosine_similarity(feature.unsqueeze(0), features_1)
            # print(similarity)
            print(i, 'and', similarity.argmax(0))

        # for camera in self.cameras:
        #     img = Image.open(camera.filename)
        #     for pred in camera.preds:
        #         bbox = pred[0]
        #         tl, br = bbox.center - bbox.size, bbox.center + bbox.size
        #         crop = img.crop((*tl, *br))
        #         plt.imshow(crop)
        #         plt.axis('off')
        #         plt.show()

        #     print('-'*30)

        return None

    @classmethod
    def from_captures(cls, f_dir, captures, object_detector, resnet, preprocess):
        cameras = [DeepCamera.from_capture(f_dir, capture, object_detector, resnet, preprocess) for capture in captures]
        
        anno_3d = [anno['values'] for anno in captures[0]['annotations'] if '3D' in anno['id']][0]
        anno_3d = [
            (anno['instanceId'], Location.from_info(anno, cameras[0].quaternion, cameras[0].position))
            for anno in anno_3d if 'Person' in anno['labelName']
        ]
        return cls(
            f_dir=f_dir,
            cameras=cameras,
            locations=anno_3d
        )
    
    def _show_bbox(self, ax, bboxes):
        for bbox in bboxes:
            ax.add_patch(
                patches.Rectangle(
                    bbox.center - bbox.size,
                    2*bbox.size[0], 2*bbox.size[1],
                    linewidth=1, edgecolor='r', facecolor='none'
                )
            )
    
    def show_gt_bboxes(self):
        # colors = ['blue', 'purple']
        colors = ['red', 'red']
        for camera in self.cameras:
            fig, ax = plt.subplots()
            ax.imshow(mpimg.imread(camera.filename))
            # self._show_bbox(ax, camera.gt_bboxes)
            for i, bbox in enumerate(camera.gt_bboxes):
                ax.add_patch(
                    patches.Rectangle(
                        bbox.center - bbox.size,
                        2*bbox.size[0], 2*bbox.size[1],
                        linewidth=2, edgecolor=colors[i], facecolor='none'
                    )
                )
            plt.axis('off')
            plt.show()
    
    def show_pred_bboxes(self):
        colors = ['blue', 'purple']
        colors = ['red', 'red']
        for camera in self.cameras:
            fig, ax = plt.subplots()
            ax.imshow(mpimg.imread(camera.filename))
            # self._show_bbox([pred[0] for pred in camera.preds])
            for i, bbox in enumerate([pred[0] for pred in camera.preds]):
                ax.add_patch(
                    patches.Rectangle(
                        bbox.center - bbox.size,
                        2*bbox.size[0], 2*bbox.size[1],
                        linewidth=2, edgecolor=colors[i], facecolor='none'
                    )
                )
            plt.axis('off')
            plt.show()