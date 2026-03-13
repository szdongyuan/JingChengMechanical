import math

import numpy as np
from scipy.ndimage import maximum_filter

from base.soundcard_audio_processor import SoundcardAudioProcessor


class AudioThdFrequencyResponseAnalysis(object):
    @staticmethod
    def spl_calculation(recorded_signal, reference_pressure=20e-6, window_size=1201, is_smooth=True):
        """
            计算录音信号的声压级（SPL）。

            参数:
                - recorded_signal : ndarray
                    输入的录音信号
                - reference_pressure : float
                    参考声压，默认为 20 μPa (20e-6 Pa)，用于 SPL 计算的基准
                - window_size: int
                    滑动窗口长度
                - is_smooth: bool
                    是否进行平滑处理，默认为 True

            返回:
                - spl_smooth : ndarray
                    经过平滑处理后的 SPL（单位 dB）
        """
        # 取绝对值并用最大值滤波器做平滑，获取每个窗口内的最大幅值
        amplitude_list = maximum_filter(np.abs(recorded_signal), size=window_size)
        # SPL 计算公式：20 * log10(幅值 / 参考声压)
        eps = 1e-20  # 极小值，避免除零
        amplitude_list = np.maximum(amplitude_list, eps)
        spl = 20 * np.log10(np.array(amplitude_list) / reference_pressure)
        if is_smooth:
            spl_smooth = np.convolve(spl, np.ones(1102) / 1102, mode='same')
            return spl_smooth
        else:
            return spl

    @staticmethod
    def calculate_average_spl(recorded_dict, channel_index):
        """
        计算平均声压级（SPL）。
        该方法录制音频数据，计算SPL曲线，然后从选定的范围内计算出平均值。
        参数:
        recorded_dict - 包含录音信息的字典。

        返回:
        True, 平均SPL值。
        """
        flag, recorded_data = SoundcardAudioProcessor().sd_rec(recorded_dict, channel_index)
        if not flag:
            msg = recorded_data
            return False, msg
        # 取recorded_dict中间1秒 数据
        sr = int(recorded_dict["sample_rate"])
        x = np.asarray(recorded_data, dtype=np.float64).reshape(-1)
        mid = x.size // 2
        half = sr // 2
        start = max(0, mid - half)
        end = min(x.size, mid + half)
        x_mid_1s = x[start:end]
        rms = AudioThdFrequencyResponseAnalysis.rms_steps(x_mid_1s)
        average_spl = 1/rms
        return True, round(average_spl, 1)

    @staticmethod
    def rms_steps(recorded_data):
        data = list(recorded_data)
        N = len(data)
        if N == 0:
            return 0.0
        squares = [x * x for x in data]  # 1) 平方
        sum_sq = sum(squares)  # 2) 相加
        mean_sq = sum_sq / N  # 3) 除以长度
        rms = math.sqrt(mean_sq)  # 4) 开根号
        return rms