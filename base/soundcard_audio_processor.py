import sounddevice as sd

from app.service.log_manager import LogManager


class SoundcardAudioProcessor(object):

    def __init__(self):
        self.logger = LogManager.set_log_handler("soundcard_core")

    @staticmethod
    def sd_rec(recorded_dict, channel_index):
        """
        使用 sounddevice 录音，并返回录音数据。
        参数:
            recorded_dict : dict
               包含录音参数的字典，例如:
               - num_frames: 录音的帧数 (默认 441000，对应 10 秒 采样)
               - sample_rate: 采样率 (默认 44100 Hz)
               - channels: 通道数 (默认单声道 1)
               - blocking: 是否阻塞模式录音 (默认 True)
               - prolong_frames: 丢弃前面若干帧 (默认 0)
        返回:
            (True, recorded_data) : tuple
               True 表示录音成功
               recorded_data 为录音后的信号数组
        """
        num_frames = recorded_dict.get("num_frames", 441000)
        sample_rate = recorded_dict.get("sample_rate", 44100)
        channels = recorded_dict.get("channels", 1)
        blocking = recorded_dict.get("blocking", True)
        prolong_frames = recorded_dict.get("prolong_frames", 0)
        try:
            # 使用 sounddevice 录音，返回的数据 shape 为 (frames, channels)，这里取第一个通道并转置
            recorded_data = sd.rec(frames=num_frames, samplerate=sample_rate, channels=channels, blocking=blocking)
            recorded_data = recorded_data.T[channel_index]
            # 如果设置了丢弃帧数，就丢掉前面的部分
            if prolong_frames > 0:
                recorded_data = recorded_data[prolong_frames:]

            return True, recorded_data
        except Exception as e:
            # 捕获 sounddevice 报错，返回友好信息
            err_msg = f"录音失败: {str(e)}"
            return False, err_msg