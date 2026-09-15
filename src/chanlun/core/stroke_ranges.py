"""合并 K 线的区间极值索引；不决定端点取舍或完成状态。"""


class StrokeRanges:
    """可追加、可重做尾部的区间极值表，查询 O(1)，追加 O(log K)。"""

    def __init__(self):
        self.highs = []
        self.lows = []
        self.count = 0

    def update(self, klines, start=0):
        if not 0 <= start <= min(self.count, len(klines)):
            raise ValueError("invalid stroke range update boundary")
        for values in (*self.highs, *self.lows):
            del values[start:]
        self.count = start
        for k in klines[start:]:
            position = self.count
            if k.index != position:
                raise ValueError("stroke ranges require consecutive merged K-line indices")
            levels = (position + 1).bit_length()
            while len(self.highs) < levels:
                self.highs.append([None] * position)
                self.lows.append([None] * position)
            self.highs[0].append(k.h)
            self.lows[0].append(k.l)
            for level in range(1, len(self.highs)):
                if level >= levels:
                    self.highs[level].append(None)
                    self.lows[level].append(None)
                    continue
                earlier = position - (1 << (level - 1))
                self.highs[level].append(max(self.highs[level - 1][position], self.highs[level - 1][earlier]))
                self.lows[level].append(min(self.lows[level - 1][position], self.lows[level - 1][earlier]))
            self.count += 1

    def query(self, start, end):
        if not 0 <= start <= end < self.count:
            return None
        level = (end - start + 1).bit_length() - 1
        left_end = start + (1 << level) - 1
        return (
            max(self.highs[level][left_end], self.highs[level][end]),
            min(self.lows[level][left_end], self.lows[level][end]),
        )
