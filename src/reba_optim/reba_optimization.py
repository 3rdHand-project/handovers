import numpy as np
from sympy.mpmath import *
from sympy import *
from .read_model import ReadModel
from .reba_assess import RebaAssess
from scipy.optimize import minimize
import time
import math
import transformations
from kinect_skeleton_publisher.joint_transformations import sympy_to_numpy, inverse

class RebaOptimization(object):
    def __init__(self, safety_dist, sum_optim=False, save_score=False, cost_factors=None):
        if cost_factors is None:
            self.cost_factors = [1, 1, 1]
        else:
            self.cost_factors = cost_factors
        self.safety_dist = safety_dist
        self.active_joints = [['spine_0', 'spine_1', 'spine_2',
                                'left_shoulder_0', 'left_shoulder_1', 'left_shoulder_2',
                                'left_elbow_0', 'left_elbow_1',
                                'left_wrist_0', 'left_wrist_1'],
                                ['spine_0', 'spine_1', 'spine_2',
                                'right_shoulder_0', 'right_shoulder_1', 'right_shoulder_2',
                                'right_elbow_0', 'right_elbow_1',
                                'right_wrist_0', 'right_wrist_1'],
                                ]
        # initialize human model
        self.model = ReadModel()
        # initialize REBA technique
        self.reba = RebaAssess(save_score=save_score, sum_optim=sum_optim)

    def get_active_joints_value(self, joints, side=0):
        joint_values = []
        for name in self.active_joints[side]:
            joint_values.append(joints[self.model.joint_names.index(name)])
        return joint_values

    def calculate_safety_cost(self, T):
        cost = 0
        for i in range(3):
            p = Float(T[i,-1])
            if p < self.safety_dist[i][0]:
                cost += abs(p - self.safety_dist[i][0])
            elif p > self.safety_dist[i][1]:
                cost += abs(p - self.safety_dist[i][1])
        return cost

    def calculate_fixed_frame_cost(self, chains, fixed_frames):     
        def caclulate_distance_to_frame(pose, ref_pose, coeffs):
            # calculate distance in position
            d_position = np.linalg.norm(pose[0]-ref_pose[0])
            # calculate distance in quaternions
            d_rotation = math.acos(2*np.inner(pose[1],ref_pose[1])**2-1)
            # return the sum of both
            return coeffs[0]*d_position + coeffs[1]*d_rotation
        def get_frame_pose(frame_name):
            key_found = False
            side = 0
            while not key_found and side < len(self.model.end_effectors):
                # check if the fixed frame is an end-effector
                if frame_name == self.model.end_effectors[side]:
                    # pose is the last transform of the chain
                    pose = chains[side][-1]
                    key_found = True
                elif frame_name in self.active_joints[side]:
                    # get the frame from the chain
                    pose = chains[side][self.active_joints[side].index(frame_name)]
                    key_found = True
            # convert the pose to tuple
            pose = transformations.m4x4_to_list(sympy_to_numpy(pose))
            return pose
        def get_pose_in_reference_frame(frame_name, frame_reference=None):
            # get frame in hip reference
            fixed = get_frame_pose(frame_name)
            # if reference frame is specified get it
            if not frame_reference is None:
                ref = get_frame_pose(frame_reference)
                # calculate the transformation between them
                link = inverse(ref)*fixed
                return link
            return fixed
        cost = 0
        if fixed_frames:
            for key in fixed_frames:
                frame_dict = fixed_frames[key]
                coeffs = frame_dict['coeffs']
                des_pose = frame_dict['desired_pose']
                ref_frame = frame_dict['reference_frame']
                # get the pose of the fixed frame
                pose = self.get_pose_in_reference_frame(key, ref_frame)
                # calculate the distance wrt to the fixed frame
                cost += caclulate_distance_to_frame(pose, des_pose, coeffs)
        return cost

    def calculate_reba_cost(self, joints):
        # convert joints to REBA norms
        reba_data = self.reba.from_joints_to_reba(joints, self.model.joint_names)
        # use the reba library to calculate the cost
        cost = self.reba.reba_optim(reba_data)
        return cost

    def assign_value(self, joint_array, dict_values):
        for key, value in dict_values.iteritems():
            joint_array[self.model.joint_names.index(key)] = value

    def return_value(self, joint_array, key):
        return joint_array[self.model.joint_names.index(key)]

    def assign_leg_values(self, joint_array):
        def assign_per_leg(side):
            knee = self.return_value(joint_array, side+'_knee')
            hip, ankle = self.model.calculate_leg_joints(knee)
            self.assign_value(joint_array, {side+'_hip_1':hip, side+'_ankle_1':hip})
        assign_per_leg('right')
        assign_per_leg('left')
        
    def cost_function(self, q, side=0, fixed_joints={}, fixed_frames={}):
        joints = q
        C_reba = 0
        C_safe = 0
        C_fixed_frame = 0
        # replace the value of fixed joints
        self.assign_value(q, fixed_joints)
        # replace the value of the leg angles using optimized knee value
        self.assign_leg_values(q)
        # check the necessity to perform the operations 
        if self.cost_factors[1] != 0 or (self.cost_factors[2] != 0 and fixed_frames):
            # first get the active joints
            active = self.get_active_joints_value(joints, side)
            # calculate the forward kinematic
            chains = self.model.forward_kinematic(active)
            if self.cost_factors[1] != 0:
                T = chains[side][-1]
                # calculate the cost based on safety distance
                C_safe = self.calculate_safety_cost(T)
            if self.cost_factors[2] != 0 and fixed_frames:
                # calculate cost based on the fixed frames
                C_fixed_frame = self.calculate_fixed_frame_cost(chains, fixed_frames)
        # calculate REBA score
        if self.cost_factors[0] != 0:
            C_reba = self.calculate_reba_cost(joints)
        # return the final score
        return self.cost_factors[0]*C_reba + self.cost_factors[1]*C_safe + self.cost_factors[2]*C_fixed_frame

    def optimize_posture(self, joints, side=0, var=0.1, fixed_joints={}, fixed_frames={}):
        # by default hip and ankles angles are fixed
        fixed_joints['right_hip_0'] = 0.
        fixed_joints['right_hip_2'] = 0.
        fixed_joints['right_ankle_0'] = 0.
        fixed_joints['left_hip_0'] = 0.
        fixed_joints['left_hip_2'] = 0.
        fixed_joints['left_ankle_0'] = 0.
        # get the joints limits for the optimization
        joint_limits = self.model.joint_limits()['limits']
        # call optimization from scipy
        res = minimize(self.cost_function, joints, args=(side, fixed_joints, fixed_frames ), method='L-BFGS-B', bounds=joint_limits, options={'eps':var})
        # replace the value of fixed joints
        for key, value in fixed_joints.iteritems():
            res.x[self.model.joint_names.index(key)] = value
        return res
