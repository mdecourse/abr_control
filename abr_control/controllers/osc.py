import numpy as np

from abr_control.utils import transformations

from .controller import Controller

class OSC(Controller):
    """ Implements an operational space controller (OSC)

    Parameters
    ----------
    robot_config : class instance
        contains all relevant information about the arm
        such as: number of joints, number of links, mass information etc.
    kp : float, optional (Default: 1)
        proportional gain term on position error
    ko : float, optional (Default: same as kp)
        proportional gain term on orientation error
    kv : float, optional (Default: None)
        derivative gain term, a good starting point is sqrt(kp)
    ki : float, optional (Default: 0)
        integral gain term
    vmax : float, optional (Default: 0.5)
        The max allowed velocity of the end-effector [meters/second].
        If the control signal specifies something above this
        value it is clipped, if set to None no clipping occurs
    ctrlr_dof : 6D list of boolean, optional (Default: position control)
        specifies which task space degrees of freedom are to be controlled
        [x, y, z, alpha, beta, gamma] (Default: [x, y, z]
        NOTE: if more ctrlr_dof are specified than degrees of freedom in
        the robotic system, the controller will perform poorly
    null_controler : Controller, optional (Default: None)
        A controller to generate a secondary control signal to be
        applied in the null space of the OSC signal (i.e. applied as much
        as possible without affecting the movement of the end-effector)
    use_g : boolean, optional (Default: True)
        calculate and compensate for the effects of gravity
    use_C : boolean, optional (Default: False)
        calculate and compensate for the Coriolis and
        centripetal effects of the arm

    Attributes
    ----------
    integrated_error : float list, optional (Default: None)
        task-space integrated error term
    """
    def __init__(self, robot_config, kp=1, ko=None, kv=None, ki=0, vmax=None,
                 ctrlr_dof=None, null_controllers=None,
                 use_g=True, use_C=False):

        super(OSC, self).__init__(robot_config)

        self.kp = kp
        self.ko = kp if ko is None else ko
        # TODO: find the appropriate default critical damping value
        # when using different position and orientation gains
        self.kv = np.sqrt(self.kp+self.ko) if kv is None else kv
        self.ki = ki
        self.vmax = vmax
        self.lamb = self.kp / self.kv
        self.null_controllers = null_controllers
        self.use_g = use_g
        self.use_C = use_C

        self.integrated_error = np.array([0.0, 0.0, 0.0])

        self.IDENTITY_N_JOINTS = np.eye(self.robot_config.N_JOINTS)

        self.ZEROS_THREE = np.zeros(3)
        self.ZEROS_SIX = np.zeros(6)

        if ctrlr_dof is None:
            ctrlr_dof = [True, True, True, False, False, False]
        self.ctrlr_dof = np.copy(ctrlr_dof)
        self.n_ctrlr_dof = np.sum(self.ctrlr_dof)

        if self.n_ctrlr_dof > robot_config.N_JOINTS:
            print('\nRobot has fewer DOF (%i) than the specified number of ' %
                  robot_config.N_JOINTS + 'space dimensions to control (%i). ' %
                  self.n_ctrlr_dof + 'Poor performance may result.\n')


    def generate(self, q, dq, target, target_vel=None,
                 ref_frame='EE', xyz_offset=None):
        """ Generates the control signal to move the EE to a target

        Parameters
        ----------
        q : float numpy.array
            current joint angles [radians]
        dq : float numpy.array
            current joint velocities [radians/second]
        target: 6 dimensional float numpy.array
            desired task space position and orientation [meters, radians]
        target_vel : float 6D numpy.array, optional (Default: None)
            desired task space velocities [meters/sec, radians/sec]
        ref_frame : string, optional (Default: 'EE')
            the point being controlled, default is the end-effector.
        xyz_offset : list, optional (Default: None)
            point of interest inside the frame of reference [meters]
        """

        assert np.array(target).shape[0] == 6

        if target_vel is None:
            target_vel = self.ZEROS_SIX
        else:
            assert np.array(target_vel).shape[0] == 6

        if xyz_offset is None:
            xyz_offset = self.ZEROS_THREE

        # calculate the Jacobian for the end effector
        J = self.robot_config.J(ref_frame, q, x=xyz_offset)
        # isolate rows of Jacobian corresponding to controlled task space DOF
        J = J[self.ctrlr_dof]

        # calculate the inertia matrix in joint space -------------------------
        M = self.robot_config.M(q)

        # calculate the inertia matrix in task space
        M_inv = np.linalg.inv(M)
        Mx_inv = np.dot(J, np.dot(M_inv, J.T))
        if abs(np.linalg.det(Mx_inv)) >= 1e-5:
            # do the linalg inverse if matrix is non-singular
            # because it's faster and more accurate
            Mx = np.linalg.inv(Mx_inv)
        else:
            # using the rcond to set singular values < thresh to 0
            # singular values < (rcond * max(singular_values)) set to 0
            Mx = np.linalg.pinv(Mx_inv, rcond=.005)

        # calculate the desired task space forces -----------------------------
        u_task = np.zeros(6)

        # calculate position error if position is being controlled
        if np.sum(self.ctrlr_dof[:3]) > 0:
            # calculate the end-effector position information
            xyz = self.robot_config.Tx(ref_frame, q, x=xyz_offset)
            u_task[:3] = xyz - target[:3]

        # calculate orientation error if orientation is being controlled
        if np.sum(self.ctrlr_dof[3:]) > 0:

            # Method 1 --------------------------------------------------------
            # transform the orientation target into a quaternion
            q_d = transformations.unit_vector(
                transformations.quaternion_from_euler(
                    target[3], target[4], target[5], axes='rxyz'))
            # get the quaternion for the end effector
            q_e = transformations.unit_vector(
                transformations.quaternion_from_matrix(
                    self.robot_config.R('EE', q)))
            # TODO: make sure that separate orientation and position gains
            # work with velocity limiting and without
            q_r = transformations.quaternion_multiply(
                q_d, transformations.quaternion_conjugate(q_e))
            u_task[3:] = -self.ko / self.kp * q_r[1:] * np.sign(q_r[0])

            # # Method 2 --------------------------------------------------------
            # # From (Caccavale et al, 1997) Section IV Quaternion feedback -----
            # # get rotation matrix for the end effector orientation
            # R_e = self.robot_config.R('EE', q)
            # # get rotation matrix for the target orientation
            # R_d = transformations.euler_matrix(
            #     target[3], target[4], target[5], axes='rxyz')[:3, :3]
            # R_ed = np.dot(R_e.T, R_d)  # eq 24
            # q_ed = transformations.unit_vector(
            #     transformations.quaternion_from_matrix(R_ed))
            # u_task[3:] = -self.ko / self.kp * np.dot(R_e, q_ed[1:])  # eq 34


        # isolate task space forces corresponding to controlled DOF
        u_task = u_task[self.ctrlr_dof]

        # implement velocity limiting -----------------------------------------
        if self.vmax is not None:
            sat = self.vmax / (self.lamb * np.abs(u_task))
            if np.any(sat < 1):
                index = np.argmin(sat)
                unclipped = self.kp * u_task[index]
                clipped = self.kv * self.vmax * np.sign(u_task[index])
                scale = (np.ones(self.n_ctrlr_dof, dtype='float32') *
                         clipped / unclipped)
                scale[index] = 1
            else:
                scale = np.ones(self.n_ctrlr_dof, dtype='float32')

            dx = np.dot(J, dq)
            u_task = -self.kv * (dx - target_vel[self.ctrlr_dof] -
                                 np.clip(sat / scale, 0, 1) *
                                 -self.lamb * scale * u_task)
            # low level signal set to zero
            u = 0.0
        else:
            # generate (x,y,z) force without velocity limiting)
            u_task *= -self.kp
            if np.all(target_vel == 0):
                # if the target velocity is zero, it's more accurate to
                # apply velocity compensation in joint space
                u = -self.kv * np.dot(M, dq)
            else:
                dx = np.dot(J, dq)
                # high level signal includes velocity compensation
                u_task -= self.kv * (dx - target_vel[self.ctrlr_dof])
                u = 0.0

        # add in integrated error term to task space forces -------------------
        if self.ki != 0:
            # add in the integrated error term
            # TODO: should this be for orientation error too? probably
            self.integrated_error += target[:3] - xyz
            u_task -= self.ki * self.integrated_error

        # transform task space control signal into joint space ----------------
        u += np.dot(J.T, np.dot(Mx, u_task))

        # add in estimation of full centrifugal and Coriolis effects ----------
        if self.use_C:
            u -= np.dot(self.robot_config.C(q=q, dq=dq), dq)

        # store the current control signal u for training in case
        # dynamics adaptation signal is being used
        # NOTE: do not include gravity or null controller in training signal
        self.training_signal = np.copy(u)

        # cancel out effects of gravity ---------------------------------------
        if self.use_g:
            # add in gravity term in joint space
            u -= self.robot_config.g(q=q)

            # add in gravity term in task space
            # Jbar = np.dot(M_inv, np.dot(J.T, Mx))
            # g = self.robot_config.g(q=q)
            # self.u_g = g
            # g_task = np.dot(Jbar.T, g)

        if self.null_controllers is not None:
            for null_controller in self.null_controllers:
                # generate control signal to apply in null space
                u_null = null_controller.generate(q, dq)
                # calculate null space filter
                Jbar = np.dot(M_inv, np.dot(J.T, Mx))
                null_filter = (self.IDENTITY_N_JOINTS - np.dot(J.T, Jbar.T))
                # add in filtered null space control signal
                u += np.dot(null_filter, u_null)

        return u
