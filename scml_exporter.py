#!/usr/bin/env python

#
# Copyright (c) 2012 by Konstantin Dmitriev <k....z...gmail.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

# pylint: disable=line-too-long
"""
Python plugin to convert the .sif format into spriter scml format
input   : FILE_NAME.sif
output  : FILE_NAME.scml
        : FILE_NAME.log

"""

import os
#import sys
import logging
import argparse
from operator import itemgetter
import xml.etree.ElementTree as ET
import image

supported_anim_types = ["offset", "scale", "angle", "spriteswitch", "pivot"]

def register_used_sprite_file(folders_list, sprite_data):
    "registers the sprite in the folders/files list. Creates the folder entry if needed"
    new_folder = True
    target_folder = {}
    folder_name = sprite_data.get("folder", "")
    for folder in folders_list:
        if folder["name"] == folder_name:
            target_folder = folder
            new_folder = False
            break

    if new_folder:
        logging.log(logging.DEBUG, "register new folder %s", folder_name)
        target_folder["name"] = folder_name
        target_folder["files"] = []
        target_folder["id"] = str(len(folders_list))
        folders_list.append(target_folder)

    new_file = True
    for tfile in target_folder["files"]:
        if tfile["name"] == sprite_data["name"]:
            new_file = False
            sprite_data["id"] = tfile["id"]
            break

    if new_file:
        logging.log(logging.DEBUG, "register new sprite file %s", sprite_data["name"])
        sprite_data["id"] = str(len(target_folder["files"]))
        target_folder["files"].append(sprite_data)


def calc_layer_edits_based_on_rect(inner_layer_data, px_ratio):
    """based on the sprite data's dimensions and tl and br entries, we can calculate the additions that should be applied to the sprite's offset and scale in spriter.
    """
    offsets = {}
    sprite_data = inner_layer_data["sprite_data"]
    tl = inner_layer_data["tl"]
    br = inner_layer_data["br"]

    # scale...
    scale = {}
    changed_width = abs(px_ratio * br["x"] - px_ratio * tl["x"])
    changed_height = abs(px_ratio * tl["y"] - px_ratio * br["y"])
    inner_layer_data["changed_width"] = changed_width
    inner_layer_data["changed_height"] = changed_height
    # compare to image width to figure out scale
    scale["x"] = changed_width / float(sprite_data["width"])
    scale["y"] = changed_height / float(sprite_data["height"])
    offsets["scale"] = scale
    # if the image rect is centralized, even if scaled via tl and br, their x or y sum should be zero. if not, we've got an offset!
    offset = {}
    offset["x"] = (br["x"] + tl["x"]) / 2
    offset["y"] = (br["y"] + tl["y"]) / 2 # in spriter, positive y is down, the opposite of synfig
    offsets["offset"] = offset

    inner_layer_data["offsets"] = offsets


def figure_out_anim_length(anim_data):
    "uses the time of the last frame, or a fallback length"
    anim_length = 100 #fallback length; min anim length
    for anim_type in supported_anim_types:
        if anim_type in anim_data:
            for wp in anim_data[anim_type]:
                converted_time = int(wp["time"] * 1000)
                anim_length = max(anim_length, converted_time)
    return anim_length

def flatten_synfig_anim_data(anim_data):
    """
From the ingested synfig anim data, return a single keyframe array, in a format more easily parsed to the spriter format
"""
    logging.log(logging.DEBUG, "flatten anim: %s", anim_data["name"])
    flattened_keyframes = []
    for anim_type in supported_anim_types:
        if anim_type in anim_data:
            for wp in anim_data[anim_type]:
                # for each synfig keyframe, we see if there isn't a flattened one for that time already.
                # if not, create a flattened keyframe for it
                converted_time = int(wp["time"] * 1000)
                flat_key = {}
                update_existing = False
                flat_key["time"] = converted_time
                for existing_flat_key in flattened_keyframes:
                    if existing_flat_key["time"] == converted_time:
                        flat_key = existing_flat_key
                        update_existing = True
                        break

                if converted_time > 0 and anim_type == "spriteswitch":
                    # spriter uses an extra keyframe for sprite swaps, with a 2 msec difference
                    extra_kf_time = converted_time - 2
                    extra_flat_key = {}
                    extra_flat_key["time"] = extra_kf_time
                    logging.log(logging.DEBUG, "add extra flat key at time: %s", str(extra_kf_time))
                    flattened_keyframes.append(extra_flat_key)

                # add info to flat key...
                flat_key[anim_type] = wp
                if not update_existing:
                    logging.log(logging.DEBUG, "add flat key at time: %s", str(flat_key["time"]))
                    flattened_keyframes.append(flat_key)

    flattened_keyframes.sort(key=itemgetter("time"))
    # spriter treats each sprite's transformation separately, while synfig's spriteswitch layer doesn't. This means we've got to figure out the transformation data where there's nothing in synfig, by interpolating
    for flat_kf in enumerate(flattened_keyframes):
        i = flat_kf[0]
        wp = flat_kf[1]
        wp_time = wp["time"]
        for anim_type in supported_anim_types:
            if anim_type not in wp:
                previous_wp = flattened_keyframes[i - 1]
                if anim_type == "spriteswitch":
                    # use sprite from previous wp
                    wp[anim_type] = previous_wp[anim_type]
                elif anim_type == "pivot":
                    # use pivot from previous wp
                    logging.log(logging.DEBUG, "flatten anim - previous pivot x: %s", str(previous_wp[anim_type]["x"]))
                    wp[anim_type] = previous_wp[anim_type]
                else:
                    # interpolate! find next valid wp... if we can't find one, use previous valid data without interpolating
                    logging.log(logging.DEBUG, "flatten anim - interpolate %s", anim_type)
                    next_valid_wp = None
                    for j in range(i + 1, len(flattened_keyframes)):
                        if anim_type in flattened_keyframes[j]:
                            next_valid_wp = flattened_keyframes[j]
                            break
                    if next_valid_wp is not None:
                        logging.log(logging.DEBUG, "flatten anim - next valid wp time is %s", str(next_valid_wp["time"]))
                        time_delta = next_valid_wp["time"] - previous_wp["time"]
                        wp[anim_type] = {}
                        if anim_type in ["offset", "scale"]:
                            for attr in ["x", "y"]:
                                interp_ratio = (next_valid_wp[anim_type][attr] - previous_wp[anim_type][attr]) / (time_delta)
                                wp[anim_type][attr] = previous_wp[anim_type][attr] + (interp_ratio * (wp_time - previous_wp["time"]))
                        elif anim_type == "angle":
                            interp_ratio = (next_valid_wp[anim_type]["value"] - previous_wp[anim_type]["value"]) / (time_delta)
                            wp[anim_type]["value"] = previous_wp[anim_type]["value"] + (interp_ratio * (wp_time - previous_wp["time"]))
                    else:
                        wp[anim_type] = previous_wp[anim_type]

    return flattened_keyframes


def parse_animated_vector_data(vector_xml_elem):
    "returns an array containing waypoints for the provided container xml element"
    transf_data_arr = []
    anim_element = vector_xml_elem.find("animated")
    if anim_element is not None:
        for wp in anim_element.iter("waypoint"):
            wp_data = {}
            wp_data["time"] = float(wp.get("time").replace("s", ""))
            wp_vec = wp.find("vector")
            wp_data["x"] = float(wp_vec.find("x").text)
            wp_data["y"] = float(wp_vec.find("y").text)
            transf_data_arr.append(wp_data)
    else:
        # single keyframe during whole anim
        wp_data = {}
        wp_data["time"] = 0.0
        wp_vec = vector_xml_elem.find("vector")
        wp_data["x"] = float(wp_vec.find("x").text)
        wp_data["y"] = float(wp_vec.find("y").text)
        transf_data_arr.append(wp_data)

    return transf_data_arr


def process(passed_args):
    "the main data ingestion and exporting process!"

    file_to_export = passed_args.infile
    file_dest = passed_args.outfile

    sif_file_dir = os.path.dirname(file_to_export)

    # Read the input file
    tree = ET.parse(file_to_export)
    canvas = tree.getroot()  # canvas

    out_root = ET.fromstring("""<?xml version="1.0" encoding="UTF-8"?>
<spriter_data scml_version="1.0" generator="BrashMonkey Spriter" generator_version="r11">
</spriter_data>
    """)

    logging.log(logging.DEBUG, out_root.tag)

    #fps = canvas.get("fps", 24)
    canvas_x = float(canvas.get("width", 500))
    #canvas_y = float(canvas.get("height", 500))
    canvas_viewbox = canvas.get("view-box", "-4.000000 2.250000 4.000000 -2.250000")
    # we can figure out the px-to-synfig units ratio using the obtained canvas dimensions
    viewbox = canvas_viewbox.split(" ")
    viewbox_width = abs(float(viewbox[0]) - float(viewbox[2]))

    px_ratio = canvas_x / viewbox_width

    scml_folders = [] # we fill the folders as we find images in the sif file
    scml_entities = []

    scml_entity = {
        "name": "entity_000"
        }

    scml_entity["anims"] = []

    scml_entities.append(scml_entity)


    for layer in canvas.iter("layer"):
        # we're assuming each switch layer in the sif file takes care of one spriter anim
        if layer.get("type") == "switch":
            anim_name = layer.get("desc")
            logging.log(logging.DEBUG, "anim_name: %s", anim_name)
            anim_data = {"name":anim_name}
            inner_layers = []
            anim_data["inner_layers"] = inner_layers
            # get image, translation changes etc
            for layer_param in layer.iter("param"):
                layer_param_type = layer_param.get("name")
                if layer_param_type == "canvas":
                    #description of inner layers of this anim/layer
                    layer_canvas = layer_param.find("canvas")
                    for canvas_layer in layer_canvas.iter("layer"): #for each inner layer...
                        cl_desc = canvas_layer.get("desc") #get layer name
                        logging.log(logging.DEBUG, "innercanvas_layer: %s", cl_desc)
                        inner_layer_data = {}
                        layersprite_data = {}
                        for canvas_layer_param in canvas_layer.iter("param"): #for each data entry of the inner layer...
                            clp_name = canvas_layer_param.get("name")
                            #logging.log(logging.DEBUG, "canvas_layer_param: " + clp_name)
                            if clp_name == "filename":
                                clp_filepath_str = canvas_layer_param.find("string").text
                                inner_layer_data["filepath"] = clp_filepath_str
                                logging.log(logging.DEBUG, "layersprite_data name: %s", clp_filepath_str)
                                inner_layer_filepath = os.path.join(sif_file_dir, clp_filepath_str)
                                inner_layer_filepath = os.path.abspath(inner_layer_filepath)
                                inner_layer_file_w, inner_layer_file_h = image.get_image_size(inner_layer_filepath)
                                layersprite_data["name"] = clp_filepath_str
                                layersprite_data["layername"] = cl_desc
                                layersprite_data["width"] = str(inner_layer_file_w)
                                layersprite_data["height"] = str(inner_layer_file_h)
                                head = os.path.dirname(clp_filepath_str)
                                layersprite_data["folder"] = str(head)
                            if clp_name in ('tl', 'br'):
                                # set up adjusted sprite rect (top left, bottom right).
                                # we can use this info to add custom scale keyframes on the spriter side
                                sprite_rect_pt = {}
                                param_vec = canvas_layer_param.find("vector")
                                sprite_rect_pt["x"] = float(param_vec.find("x").text)
                                sprite_rect_pt["y"] = float(param_vec.find("y").text)
                                inner_layer_data[clp_name] = sprite_rect_pt
                        # finalize inner layer: link data, make necessary calculations
                        if layersprite_data["name"]:
                            inner_layers.append(inner_layer_data)
                            inner_layer_data["name"] = cl_desc
                            register_used_sprite_file(scml_folders, layersprite_data)
                            inner_layer_data["sprite_data"] = layersprite_data
                            calc_layer_edits_based_on_rect(inner_layer_data, px_ratio)

                elif layer_param_type == "transformation":
                    #description of movements, scale changes etc
                    layer_composite = layer_param.find("composite")
                    for transformation in layer_composite: #for each transformation type...
                        transf_type = transformation.tag
                        logging.log(logging.DEBUG, "transf_type: %s", transf_type)
                        if transf_type in ("offset", "scale"):
                            anim_data[transf_type] = parse_animated_vector_data(transformation)
                        elif transf_type == "angle":
                            transf_data_arr = []
                            anim_element = transformation.find("animated")
                            if anim_element is not None:
                                for wp in anim_element.iter("waypoint"):
                                    wp_data = {}
                                    wp_data["time"] = float(wp.get("time").replace("s", ""))
                                    wp_angle = wp.find("angle")
                                    wp_data["value"] = float(wp_angle.get("value"))
                                    transf_data_arr.append(wp_data)
                            else:
                                # single keyframe during whole anim
                                wp_data = {}
                                wp_data["time"] = 0.0
                                wp_angle = transformation.find("angle")
                                wp_data["value"] = float(wp_angle.get("value"))
                                transf_data_arr.append(wp_data)
                            anim_data[transf_type] = transf_data_arr

                elif layer_param_type == "origin":
                    anim_data["pivot"] = parse_animated_vector_data(layer_param)

                elif layer_param_type == "layer_name":
                    #description of image shown by this switch layer, and its changes, if animated
                    transf_data_arr = []
                    anim_element = layer_param.find("animated")
                    if anim_element is not None:
                        for wp in anim_element.iter("waypoint"):
                            if wp.get("time") != "SOT":
                                wp_data = {}
                                wp_data["time"] = float(wp.get("time").replace("s", ""))
                                wp_data["layer"] = wp.find("string").text
                                transf_data_arr.append(wp_data)

                    else:
                        # single keyframe describing layer used during whole anim
                        wp_data = {}
                        wp_data["time"] = 0.0
                        wp_data["layer"] = layer_param.find("string").text
                        transf_data_arr.append(wp_data)
                    anim_data["spriteswitch"] = transf_data_arr
            scml_entity["anims"].append(anim_data)

    # done gathering data!
    # it's time to write it down in the out file
    # write folders and imgs...
    logging.log(logging.DEBUG, "done gathering data!")
    for scmlfolder in scml_folders:
        folder_xml = ET.Element("folder", {"id":scmlfolder["id"],"name":scmlfolder["name"]})
        out_root.append(folder_xml)
        for folderfile in scmlfolder["files"]:
            file_xml = ET.Element("file", {k:folderfile[k] for k in ["id","name","width","height"]})
            file_xml.attrib["pivot_x"] = "0"
            file_xml.attrib["pivot_y"] = "1"
            folder_xml.append(file_xml)

        ent_index = 0
        for ent in scml_entities:
            ent_xml = ET.Element("entity", {"id":str(ent_index),"name":ent["name"]})
            anim_index = 0
            for anim in ent["anims"]:
                anim_xml = ET.Element("animation", {"id":str(anim_index),"name":anim["name"],"interval":"100"}) #i don't know what interval is, but i've only seen it set to 100
                anim_length = figure_out_anim_length(anim)
                anim_xml.attrib["length"] = str(anim_length)
                # anims have one mainline and multiple timeline tags.
                # the mainline tag declares all keyframes of the timelines, ordered by time.
                # about the timelines tags, there seems to be one per sprite used, and, in each keyframe, time and all transformations and sprite link (folder+id) are declared.
                # this means the actual anim data is in the timelines.
                anim_mainline = ET.Element("mainline")
                anim_xml.append(anim_mainline)
                # we've got to "flatten" the synfig keyframes, because each animated data entry has their own timeline there
                flat_keyframes = flatten_synfig_anim_data(anim)
                sprite_timelines = {}
                for flat_kf in enumerate(flat_keyframes):
                    #i = flat_kf[0]
                    kf = flat_kf[1]
                    kf_layer = kf["spriteswitch"]["layer"]
                    if kf_layer not in sprite_timelines:
                        timeline_xml = ET.Element("timeline", {"id":str(len(sprite_timelines)),"name":kf_layer})
                        sprite_timelines[kf_layer] = timeline_xml
                        anim_xml.append(timeline_xml)
                    timeline_xml = sprite_timelines[kf_layer]
                    # add key to mainline, now that we've assured a timeline exists
                    mainline_key_xml = ET.Element("key", {"id":str(len(anim_mainline)), "time":str(kf["time"])})
                    # TODO we're only supporting one object ref per anim!
                    objref_xml = ET.Element("object_ref", {"id":"0", "timeline":timeline_xml.attrib["id"], "key":str(len(timeline_xml)), "z_index":"0"})
                    mainline_key_xml.append(objref_xml)
                    anim_mainline.append(mainline_key_xml)
                    # add key to timeline
                    timeline_key_xml = ET.Element("key", {"id":str(len(timeline_xml)), "time":str(kf["time"]), "spin":"0"})
                    # figure out sprite's folder and file id
                    folder_id = "0"
                    file_id = "0"
                    anim_layers = anim["inner_layers"]
                    frame_anim_layer = {}
                    for anim_layer in anim_layers:
                        layer_sprite_data = anim_layer["sprite_data"]
                        logging.log(logging.DEBUG, "layersprite_data name: " + layer_sprite_data["layername"] + " kf layer: " + kf_layer)
                        if layer_sprite_data["layername"] == kf_layer:
                            for scmlfolder in scml_folders:
                                # find folder's id
                                if scmlfolder["name"] == layer_sprite_data["folder"]:
                                    logging.log(logging.DEBUG, "got anim sprite folder and file!")
                                    folder_id = scmlfolder["id"]
                                    file_id = layer_sprite_data["id"]
                                    frame_anim_layer = anim_layer
                                    break

                    layer_offsets = frame_anim_layer["offsets"]
                    # convert pivot info to a value relative to the sprite's size and offsets
                    logging.log(logging.DEBUG, "kf pivot x before conv: %s", str(kf["pivot"]["x"]))
                    conv_pivot = kf["pivot"].copy()
                    conv_pivot["x"] = 0.5 + ((px_ratio * (conv_pivot["x"])) / float(frame_anim_layer["changed_width"]))
                    conv_pivot["y"] = 0.5 + ((px_ratio * (conv_pivot["y"])) / float(frame_anim_layer["changed_height"]))

                    timeline_obj_xml = ET.Element("object", {"folder":folder_id, "file":file_id, "x":str(px_ratio * (kf["offset"]["x"] + layer_offsets["offset"]["x"])), "y":str(px_ratio * (kf["offset"]["y"] + layer_offsets["offset"]["y"])), "scale_x":str(kf["scale"]["x"] * layer_offsets["scale"]["x"]), "scale_y":str(kf["scale"]["y"] * layer_offsets["scale"]["y"]), "angle":str(kf["angle"]["value"]), "pivot_x":str(conv_pivot["x"]), "pivot_y":str(conv_pivot["y"])})
                    timeline_key_xml.append(timeline_obj_xml)
                    timeline_xml.append(timeline_key_xml)
                ent_xml.append(anim_xml)
                anim_index += 1
            out_root.append(ent_xml)
            ent_index += 1



    logging.log(logging.DEBUG, "xml set up, writing now!")
    with open(file_dest, "w", encoding="utf-8") as fil:
        xml_header = """<?xml version="1.0" encoding="UTF-8"?>
{content}
"""
        fil.write(xml_header.format(content=ET.tostring(out_root, "unicode")))
    logging.log(logging.DEBUG, "DONE!")



parser = argparse.ArgumentParser()
parser.add_argument("infile")
parser.add_argument("outfile")
ns = parser.parse_args()

logging.basicConfig(filename=ns.infile + ".log", format='%(name)s - %(levelname)s - %(message)s')
logging.getLogger().setLevel(logging.DEBUG)
logging.log(logging.DEBUG, "log start! exporting %s", ns.infile)

process(ns)

logging.log(logging.DEBUG, "log end! exporting %s", ns.infile)
