# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# This file incorporates work covered by the following copyright and
# permission notice:
#
#   Copyright 2019 Google LLC
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import asyncio
import base64
import json
import logging
import re
from subprocess import Popen, PIPE

import gi
gi.require_version("Gst", "1.0")
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')
from gi.repository import Gst
from gi.repository import GstWebRTC
from gi.repository import GstSdp

logger = logging.getLogger("gstwebrtc_app")
logger.setLevel(logging.INFO)

class GSTWebRTCAppError(Exception):
    pass


class GSTWebRTCApp:
    def __init__(self, stun_servers=None, turn_servers=None, encoder=None):
        """Initialize GStreamer WebRTC app.

        Initializes GObjects and checks for required plugins.

        Arguments:
            stun_servers {[list of string]} -- Optional STUN server uris in the form of:
                                    stun:<host>:<port>
            turn_servers {[list of strings]} -- Optional TURN server uris in the form of:
                                    turn://<user>:<password>@<host>:<port>
        """

        self.stun_servers = stun_servers
        self.turn_servers = turn_servers
        self.pipeline = None
        self.webrtcbin = None
        self.encoder = encoder

        self.peer_connection_state = None
        self.ice_connection_state = None

        self.fakesink_state = None
        self.fakesink = None
        self.rtpqueue_state = None
        self.rtpqueue = None

        # WebRTC ICE and SDP events
        self.on_ice = lambda mlineindex, candidate: logger.warn(
            'unhandled ice event')
        self.on_sdp = lambda sdp_type, sdp: logger.warn('unhandled sdp event')

        Gst.init(None)

        self.check_plugins()

        self.pipeline = None

    # [START build_webrtcbin_pipeline]
    def build_webrtcbin_pipeline(self):
        """Adds the webrtcbin elments to the pipeline.

        The video and audio pipelines are linked to this in the
            build_video_pipeline() and build_audio_pipeline() methods.
        """

        # Create webrtcbin element named app
        self.webrtcbin = Gst.ElementFactory.make("webrtcbin", "app")

        # The bundle policy affects how the SDP is generated.
        # This will ultimately determine how many tracks the browser receives.
        # Setting this to max-compat will generate separate tracks for
        # audio and video.
        # See also: https://webrtcstandards.info/sdp-bundle/
        self.webrtcbin.set_property("bundle-policy", "max-compat")

        # Connect signal handlers
        self.webrtcbin.connect(
            'on-negotiation-needed', lambda webrtcbin: self.__on_negotiation_needed(webrtcbin))
        self.webrtcbin.connect('on-ice-candidate', lambda webrtcbin, mlineindex,
                               candidate: self.__send_ice(webrtcbin, mlineindex, candidate))
        
    
        self.webrtcbin.connect('pad-added', self.handle_webcam_stream)

        # Add STUN server
        # TODO: figure out how to add more than 1 stun server.
        if self.stun_servers:
            self.webrtcbin.set_property("stun-server", self.stun_servers[0])

        # Add TURN server
        if self.turn_servers:
            for turn_server in self.turn_servers:
                logger.info("adding TURN server: %s" % turn_server)
                self.webrtcbin.emit("add-turn-server", turn_server)

        # Add element to the pipeline.
        self.pipeline.add(self.webrtcbin)
    # [END build_webrtcbin_pipeline]
        


    def check_plugins(self):
        """Check for required gstreamer plugins.

        Raises:
            GSTWebRTCAppError -- thrown if any plugins are missing.
        """

        required = ["opus", "nice", "webrtc", "dtls", "srtp", "rtp", "sctp",
                    "rtpmanager"]

        # supported = ["nvh264enc", "vp8enc", "vp9enc", "x264enc"]
        # if self.encoder not in supported:
        #     raise GSTWebRTCAppError('Unsupported encoder, must be one of: ' + ','.join(supported))

        # if self.encoder.startswith("nv"):
        #     required.append("nvcodec")

        # if self.encoder.startswith("vp"):
        #     required.append("vpx")

        missing = list(
            filter(lambda p: Gst.Registry.get().find_plugin(p) is None, required))
        if missing:
            raise GSTWebRTCAppError('Missing gstreamer plugins:', missing)

    def set_sdp(self, sdp_type, sdp):
        """Sets remote SDP received by peer.

        Arguments:
            sdp_type {string} -- type of sdp, offer or answer
            sdp {object} -- SDP object

        Raises:
            GSTWebRTCAppError -- thrown if SDP is recevied before session has been started.
            GSTWebRTCAppError -- thrown if SDP type is not 'answer', this script initiates the call, not the peer.
        """

        if not self.webrtcbin:
            raise GSTWebRTCAppError('Received SDP before session started')

        if sdp_type != 'answer':
            raise GSTWebRTCAppError('ERROR: sdp type was not "answer"')

        _, sdpmsg = GstSdp.SDPMessage.new_from_text(sdp)
        answer = GstWebRTC.WebRTCSessionDescription.new(
            GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
        promise = Gst.Promise.new()
        self.webrtcbin.emit('set-remote-description', answer, promise)
        promise.interrupt()

    def set_ice(self, mlineindex, candidate):
        """Adds ice candidate received from signalling server

        Arguments:
            mlineindex {integer} -- the mlineindex
            candidate {string} -- the candidate

        Raises:
            GSTWebRTCAppError -- thrown if called before session is started.
        """

        logger.info("setting ICE candidate: %d, %s" % (mlineindex, candidate))

        if not self.webrtcbin:
            raise GSTWebRTCAppError('Received ICE before session started')

        self.webrtcbin.emit('add-ice-candidate', mlineindex, candidate)


    def __on_offer_created(self, promise, _, __):
        """Handles on-offer-created promise resolution

        The offer contains the local description.
        Generate a set-local-description action with the offer.
        Sends the offer to the on_sdp handler.

        Arguments:
            promise {GstPromise} -- the promise
            _ {object} -- unused
            __ {object} -- unused
        """

        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        promise = Gst.Promise.new()
        self.webrtcbin.emit('set-local-description', offer, promise)
        promise.interrupt()
        loop = asyncio.new_event_loop()
        sdp_text = offer.sdp.as_text()
        # rtx-time needs to be set to 125 milliseconds for optimal performance
        if 'rtx-time' not in sdp_text:
            logger.warning("injecting rtx-time to SDP")
            sdp_text = re.sub(r'(apt=\d+)', r'\1;rtx-time=125', sdp_text)
        elif 'rtx-time=125' not in sdp_text:
            logger.warning("injecting modified rtx-time to SDP")
            sdp_text = re.sub(r'rtx-time=\d+', r'rtx-time=125', sdp_text)
        #Firefox needs profile-level-id=42e01f in the offer, but webrtcbin does not add this.
        #TODO: Remove when fixed in webrtcbin.
        # https://gitlab.freedesktop.org/gstreamer/gstreamer/-/issues/1106
        if '264' in self.encoder:
            if 'profile-level-id' not in sdp_text:
                logger.warning("injecting profile-level-id to SDP")
                sdp_text = sdp_text.replace('packetization-mode=1', 'profile-level-id=42e01f;packetization-mode=1')
            if 'level-asymmetry-allowed' not in sdp_text:
                logger.warning("injecting level-asymmetry-allowed to SDP")
                sdp_text = sdp_text.replace('packetization-mode=1', 'level-asymmetry-allowed=1;packetization-mode=1')
        loop.run_until_complete(self.on_sdp('offer', sdp_text))

    def __on_negotiation_needed(self, webrtcbin):
        """Handles on-negotiation-needed signal, generates create-offer action

        Arguments:
            webrtcbin {GstWebRTCBin gobject} -- webrtcbin gobject
        """

        logger.info("handling on-negotiation-needed, creating offer.")
        promise = Gst.Promise.new_with_change_func(
            self.__on_offer_created, webrtcbin, None)
        webrtcbin.emit('create-offer', None, promise)

    def __send_ice(self, webrtcbin, mlineindex, candidate):
        """Handles on-ice-candidate signal, generates on_ice event

        Arguments:
            webrtcbin {GstWebRTCBin gobject} -- webrtcbin gobject
            mlineindex {integer} -- ice candidate mlineindex
            candidate {string} -- ice candidate string
        """

        logger.debug("received ICE candidate: %d %s", mlineindex, candidate)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.on_ice(mlineindex, candidate))
    

    def build_webcam_video_pipeline(self):
        """As the webrtcbin needs to have codecs it support beforehand for generating SDP. So when streaming 
           video and audio to browser, the data is readily available on the server side, so we can link the elements
           like ximagesrc and set caps which helps webrtcbin to take care of configuring transceivers. But in this case of 
           webcam, as we're generating SDP, we need to configure the transceiver with preferred codecs.

           Remember, only after adding the media/tracks to webrtc then the negotiation starts thus SD generations begins.
           If you remember the logs of webrtcbin(debug) it showed the sdp is getting from transceiver...***
        """
        # codec_caps.set_value("rtcp-fb-nack-pli", True)
        # codec_caps.set_value("rtcp-fb-ccm-fir", True)
        # codec_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)
        # codec_caps.set_value("aggregate-mode", "zero-latency")

        codec_caps = Gst.caps_from_string("application/x-rtp")
        codec_caps.set_value("media", "video")
        codec_caps.set_value("encoding-name", "H264")
        codec_caps.set_value("payload", 123)
        codec_caps.set_value("clock-rate", 90000)
        # codec_caps.set_value("rtcp-fb-nack-pli", True)
        # codec_caps.set_value("rtcp-fb-ccm-fir", True)
        # codec_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)
        #codec_caps.set_value("aggregate-mode", "zero-latency")

        # add the transceiver
        self.webrtcbin.emit("add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY, codec_caps)

    def handle_webcam_stream(self, webrtcbin, pad):
        pad_name = pad.get_name()
        logger.info("Pad Name: " + str(pad_name))

        if "sink" not in pad_name:
            logger.info("It's a src pad")
            caps = pad.get_current_caps()
            logger.info("Pad caps: " + str(caps))

            fakesink = Gst.ElementFactory.make("fakesink", "fakesinkbroo")
            self.pipeline.add(fakesink)
            if not Gst.Element.link(self.webrtcbin, fakesink):
                raise GSTWebRTCAppError("Failed to raise webrtcbin -> fakesink")


            # self.rtpqueue = Gst.ElementFactory.make("queue", "rtpqueue")
            # rtph264depay = Gst.ElementFactory.make("rtph264depay", "rtph264depaybroo")
            # rtph264_caps = Gst.caps_from_string("application/x-rtp")
            # rtph264_caps.set_value("media", "video")
            # rtph264_caps.set_value("encoding-name", "H264")
            # rtph264_caps.set_value("payload", 123)
            # rtph264_caps.set_value("rtcp-fb-nack-pli", True)
            # rtph264_caps.set_value("rtcp-fb-ccm-fir", True)
            # rtph264_caps.set_value("rtcp-fb-x-gstreamer-fir-as-repair", True)
            # rtph264_caps.set_value("aggregate-mode", "zero-latency")

            # self.fakesink = Gst.ElementFactory.make("fakesink", "fakesinkbroo")

            # rtph264capsfilter = Gst.ElementFactory.make("capsfilter")
            # rtph264capsfilter.set_property("caps", rtph264_caps)
            

            # # v4l2sink = Gst.ElementFactory.make("v4l2sink", "v4l2sinkbroo")
            # # v4l2sink.set_property("device", "/dev/video9")

            # self.pipeline.add(self.rtpqueue)
            # self.pipeline.add(rtph264capsfilter)
            # self.pipeline.add(rtph264depay)
            # self.pipeline.add(self.fakesink)
            
            # # self.pipeline.add(v4l2sink)

            # if not Gst.Element.link(self.webrtcbin, self.rtpqueue):
            #     raise GSTWebRTCAppError("Failed to link webrtcbin -> rtpqueue")
            
            # if not Gst.Element.link(self.rtpqueue, rtph264capsfilter):
            #     raise GSTWebRTCAppError("Failed to link rtpqueue -> rtph264capsfilter")
            
            # if not Gst.Element.link(rtph264capsfilter, rtph264depay):
            #     raise GSTWebRTCAppError("Failed to link rtph264capsfilter -> rtph264depay")
            
            # if not Gst.Element.link(rtph264depay, self.fakesink):
            #     raise GSTWebRTCAppError("Failed to raise rtph264depay -> fakesink")
            
            # if not Gst.Element.link(rtph264depay, v4l2sink):
            #     raise GSTWebRTCAppError("Failed to link rtph265depay -> v4l2sink")


    def start_pipeline(self):
        """Starts the GStreamer pipeline
        """

        logger.info("starting pipeline")

        self.pipeline = Gst.Pipeline.new()

        # Construct the webrtcbin pipeline with video and audio.
        self.build_webrtcbin_pipeline()
        self.build_webcam_video_pipeline()

        # Advance the state of the pipeline to PLAYING.
        res = self.pipeline.set_state(Gst.State.PLAYING)
        if res.value_name != 'GST_STATE_CHANGE_SUCCESS':
            raise GSTWebRTCAppError(
                "Failed to transition pipeline to PLAYING: %s" % res)


        # Enable NACKs on the transceiver, helps with retransmissions and freezing when packets are dropped.
        # transceiver = self.webrtcbin.emit("get-transceiver", 0)
        # transceiver.set_property("do-nack", True)

        logger.info("pipeline started")

    def bus_call(self, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            logger.error("End-of-stream\n")
            return False
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error("Error: %s: %s\n" % (err, debug))
            return False
        elif t == Gst.MessageType.STATE_CHANGED:
            if isinstance(message.src, Gst.Pipeline):
                old_state, new_state, pending_state = message.parse_state_changed()
                logger.info(("Pipeline state changed from %s to %s." %
                    (old_state.value_nick, new_state.value_nick)))
                if (old_state.value_nick == "paused" and new_state.value_nick == "ready"):
                    logger.info("stopping bus message loop")
                    return False
        elif t == Gst.MessageType.LATENCY:
            if self.pipeline:
                try:
                    self.pipeline.recalculate_latency()
                except Exception as e:
                    logger.warning("failed to recalculate warning, exception: %s" % str(e))

        return True

    async def handle_bus_calls(self):
        # Start bus call loop
        running = True
        bus = None
        while running:
            if self.pipeline is not None:
                bus = self.pipeline.get_bus()
            if bus is not None:
                while bus.have_pending():
                    msg = bus.pop()
                    if not self.bus_call(msg):
                        running = False
            await asyncio.sleep(0.1)

    async def check_property(self):
        while True:
            if self.pipeline is not None:
                if self.webrtcbin is not None:

                    curr_ice_state = self.webrtcbin.get_property("ice-connection-state")
                    if curr_ice_state.value_name != self.ice_connection_state:
                        logger.info("Ice connection state: " + str(curr_ice_state.value_name))
                        self.ice_connection_state = curr_ice_state.value_name

                    curr_state = self.webrtcbin.get_property("connection-state")
                    if curr_state.value_name != self.peer_connection_state:
                        logger.info("Peer connection state: " + str(curr_state.value_name))
                        self.peer_connection_state = curr_state.value_name
                    
                    if self.fakesink is not None:
                        state_change_return, fakesink_curr_state, pending_state = self.fakesink.get_state(Gst.CLOCK_TIME_NONE)
                        if self.fakesink_state != fakesink_curr_state:
                            logger.info("Current state:" + str(fakesink_curr_state.value_nick))
                            logger.info("state change return: " + str(state_change_return.value_nick))
                            logger.info("pending state: " + str(pending_state.value_nick))

                            self.fakesink_state = fakesink_curr_state
                        
                    # if self.rtpqueue is not None:
                    #     state_change_return, rtp_curr_state, pending_state = self.rtpqueue.get_state(Gst.CLOCK_TIME_NONE)
                    #     if  self.rtpqueue_state != rtp_curr_state:
                    #         logger.info("RTP Current state:" + str(rtp_curr_state.value_nick))
                    #         logger.info("state change return: " + str(state_change_return.value_nick))
                    #         logger.info("pending state: " + str(pending_state.value_nick))

                    #         self.rtpqueue_state = rtp_curr_state

            await asyncio.sleep(0.1)

    def stop_pipeline(self):
        logger.info("stopping pipeline")
        if self.pipeline:
            logger.info("setting pipeline state to NULL")
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.unparent()
            self.pipeline = None
            logger.info("pipeline set to state NULL")
        if self.webrtcbin:
            self.webrtcbin.set_state(Gst.State.NULL)
            self.webrtcbin.unparent()
            self.webrtcbin = None
            logger.info("webrtcbin set to state NULL")
        if self.fakesink:
            self.fakesink.set_state(Gst.State.NULL)
            self.fakesink.unparent()
            self.fakesink = None
        logger.info("pipeline stopped")
