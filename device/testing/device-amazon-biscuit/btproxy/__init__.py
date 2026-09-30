# SPDX-License-Identifier: MIT
#
# api_pb2.py and api_options_pb2.py in this directory are copied unmodified
# from aioesphomeapi 45.3.1 (https://github.com/esphome/aioesphomeapi),
# which is distributed under the following licence:
#
# MIT License
#
# Copyright (c) 2018 Otto Winter
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Just enough of aioesphomeapi to encode ESPHome API messages.

Two generated protobuf modules, vendored rather than depending on
aioesphomeapi itself: that package pulls in noiseprotocol, cryptography and
its own async client, none of which a passive proxy needs. These two files
plus py3-protobuf (1.3 MB) are the whole dependency, which is what lets the
proxy ship in the core package instead of behind the voice bundle.
"""
