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

import argparse
#import sys
import logging
import math
import copy
import os
import xml.etree.ElementTree as ET
from operator import itemgetter

import image

supported_anim_types = ["offset", "scale", "angle", "spriteswitch", "pivot"]


def get_layer_data_by_id(layer_id, anim_data):
    "returns the layer data dict, or None if not found"
    inner_layers = anim_data["inner_layers"]
    for il in inner_layers:
        if il["id"] == layer_id:
            return il

    return None

def get_anim_root_layer(anim_data):
    "every anim has a single root layer, parenting all the anim's layers. This func returns it!"
    inner_layers = anim_data["inner_layers"]
    for il in inner_layers:
        if il["parent_layer_id"] == "none":
            return il

    logging.log(logging.ERROR, "couldn't find root layer for anim %s", anim_data["name"])
    return None

def get_child_layers_ids(parent_layer_id, anim_data):
    "returns a set of ids"
    children_ids = set()

    inner_layers = anim_data["inner_layers"]
    for il in inner_layers:
        if il["parent_layer_id"] == parent_layer_id:
            children_ids.add(il["id"])

    return children_ids

def register_used_sprite_file(context, sprite_data):
    "registers the sprite in the folders/files list. Creates the folder entry if needed"
    new_folder = True
    target_folder = {}
    folders_list = context["scml_folders"]
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
            sprite_data["folder_id"] = target_folder["id"]
            break

    if new_file:
        logging.log(logging.DEBUG, "register new sprite file %s", sprite_data["name"])
        sprite_data["id"] = str(len(target_folder["files"]))
        sprite_data["folder_id"] = target_folder["id"]
        target_folder["files"].append(sprite_data)


def calc_layer_edits_based_on_rect(inner_layer_data, context):
    """based on the sprite data's dimensions and tl and br entries, we can calculate the additions that should be applied to the sprite's offset and scale in spriter.
    """
    offsets = {}
    px_ratio = context["px_ratio"]
    sprite_data = inner_layer_data["sprite_data"]

    if "tl" not in inner_layer_data:
        # this is not a sprite layer, abort
        return

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

def get_empty_anim_layer_transformation_data():
    "returns transformation data that a layer without any transformation would have"
    ret_data = {}

    for anim_type in supported_anim_types:
        if anim_type in ["offset", "pivot"]:
            ret_data[anim_type] = [{"time":0.0,"x":0.0,"y":0.0}]
        elif anim_type == "scale":
            ret_data[anim_type] = [{"time":0.0,"x":1.0,"y":1.0}]
        elif anim_type == "angle":
            ret_data[anim_type] = [{"time":0.0,"value":0.0}]
        elif anim_type == "spriteswitch":
            ret_data[anim_type] = [{"time":0.0,"layer":""}]

    return ret_data


def process_layer_canvas(parent_layer_data, this_layer, anim_data, context):
    "ingest data from layers recursively"

    this_layer_data = {}
    this_layer_data["id"] = str(context["next_layer_id"])
    this_layer_data["is_spriter_object"] = False # by default, we don't create objects for any layers except sprite import
    context["next_layer_id"] += 1

    this_layer_data["type"] = this_layer.get("type")

    # get layer name and its parent layer id
    if parent_layer_data is not None:
        this_layer_data["parent_layer_id"] = parent_layer_data["id"]
        this_layer_data["name"] = this_layer.get("desc", this_layer.get("type"))
    else:
        this_layer_data["parent_layer_id"] = "none"
        this_layer_data["name"] = "root_layer"

    logging.log(logging.DEBUG, "parsing layer: %s - its id will be %s", this_layer_data["name"], this_layer_data["id"])

    layersprite_data = {}
    this_layer_data["sprite_data"] = layersprite_data

    layer_kf_data = get_empty_anim_layer_transformation_data()
    this_layer_data["keyframes_data"] = layer_kf_data

    # get image, translation changes etc
    for layer_child in this_layer:
        if layer_child.tag == "param":
            layer_param_type = layer_child.get("name")
            if layer_param_type == "canvas":
                #description of inner layers of this anim/layer
                layer_canvas = layer_child.find("canvas")
                for canvas_child in layer_canvas: #for each inner layer...
                    if canvas_child.tag == "layer":
                        process_layer_canvas(this_layer_data, canvas_child, anim_data, context)

            elif layer_param_type == "filename":
                clp_filepath_str = layer_child.find("string").text
                this_layer_data["filepath"] = clp_filepath_str
                logging.log(logging.DEBUG, "layersprite_data name: %s", clp_filepath_str)
                inner_layer_filepath = os.path.join(context["sif_file_dir"], clp_filepath_str)
                inner_layer_filepath = os.path.abspath(inner_layer_filepath)
                inner_layer_file_w, inner_layer_file_h = image.get_image_size(inner_layer_filepath)
                layersprite_data["name"] = clp_filepath_str
                layersprite_data["layername"] = this_layer_data["name"]
                layersprite_data["width"] = str(inner_layer_file_w)
                layersprite_data["height"] = str(inner_layer_file_h)
                head = os.path.dirname(clp_filepath_str)
                layersprite_data["folder"] = str(head)

            elif layer_param_type in ('tl', 'br'):
                # set up adjusted sprite rect (top left, bottom right).
                # we can use this info to add custom scale keyframes on the spriter side
                sprite_rect_pt = {}
                param_vec = layer_child.find("vector")
                sprite_rect_pt["x"] = float(param_vec.find("x").text)
                sprite_rect_pt["y"] = float(param_vec.find("y").text)
                this_layer_data[layer_param_type] = sprite_rect_pt

            elif layer_param_type == "transformation":
                #description of movements, scale changes etc
                layer_composite = layer_child.find("composite")
                for transformation in layer_composite: #for each transformation type...
                    transf_type = transformation.tag
                    logging.log(logging.DEBUG, "transf_type: %s", transf_type)
                    if transf_type in ("offset", "scale"):
                        layer_kf_data[transf_type] = parse_animated_vector_data(transformation)
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
                        layer_kf_data[transf_type] = transf_data_arr

            elif layer_param_type == "origin":
                layer_kf_data["pivot"] = parse_animated_vector_data(layer_child)

            elif layer_param_type == "layer_name":
                #description of image shown by this switch layer, and its changes, if animated
                transf_data_arr = []
                anim_element = layer_child.find("animated")
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
                    wp_data["layer"] = layer_child.find("string").text
                    transf_data_arr.append(wp_data)
                layer_kf_data["spriteswitch"] = transf_data_arr

    # finalize inner layer: link data, make necessary calculations
    anim_data["inner_layers"].append(this_layer_data)
    if "name" in layersprite_data:
        register_used_sprite_file(context, layersprite_data)
        calc_layer_edits_based_on_rect(this_layer_data, context)
        # register this layer as sprite object
        this_layer_data["is_spriter_object"] = True

    logging.log(logging.DEBUG, "-layer %s (%s) anims: %s", this_layer_data["name"], this_layer_data["id"], this_layer_data["keyframes_data"])
    logging.log(logging.DEBUG, "-done parsing layer: %s (%s)", this_layer_data["name"], this_layer_data["id"])


def flatten_synfig_anim_data(anim_data):
    "From the ingested synfig anim data, return a single keyframe array, in a format more easily parsed to the spriter format"
    logging.log(logging.DEBUG, "flatten anim: %s", anim_data["name"])
    flattened_keyframes = []
    for il in anim_data["inner_layers"]:
        # for each inner layer...
        layer_kf_data = il["keyframes_data"]
        for anim_type in supported_anim_types:
            if anim_type in layer_kf_data:
                for wp in layer_kf_data[anim_type]:
                    # for each synfig keyframe, we see if there isn't a flattened one for that time already.
                    # if not, create a flattened keyframe for it
                    converted_time = int(wp["time"] * 1000)
                    flat_key = next((fkf for fkf in flattened_keyframes if fkf["time"] == converted_time), { "layers": {}, "time": converted_time, "is_new": True })
                    update_existing = not flat_key["is_new"]
                    flat_key["is_new"] = False

                    if converted_time > 0 and anim_type == "spriteswitch":
                        # spriter uses an extra keyframe for sprite swaps, with a 2 msec difference
                        extra_kf_time = converted_time - 2
                        extra_flat_key = next((fkf for fkf in flattened_keyframes if fkf["time"] == extra_kf_time), { "layers": { il["id"]: {} }, "time": extra_kf_time, "is_new": True })
                        if extra_flat_key["is_new"]:
                            logging.log(logging.DEBUG, "add extra flat key at time: %s", str(extra_kf_time))
                            flattened_keyframes.append(extra_flat_key)
                            extra_flat_key["is_new"] = False
                        else:
                            if il["id"] not in extra_flat_key["layers"]:
                                extra_flat_key["layers"][il["id"]] = {}

                    # add info to flat key...
                    # create layer's entry in flat key if it doesn't exist
                    layer_in_fk = {}
                    flat_layers = flat_key["layers"]
                    if il["id"] not in flat_layers:
                        flat_layers[il["id"]] = layer_in_fk
                    else:
                        layer_in_fk = flat_layers[il["id"]]

                    layer_in_fk[anim_type] = wp
                    if not update_existing:
                        logging.log(logging.DEBUG, "add flat key at time: %s", str(flat_key["time"]))
                        flattened_keyframes.append(flat_key)

    return add_hierarchy_influence_to_flattened_keyframes(anim_data,  cleanup_flattened_keyframes(anim_data, flattened_keyframes))


def cleanup_flattened_keyframes(anim_data, flattened_keyframes):
    "sorts and creates missing data in the flattened keyframes as needed"
    flattened_keyframes.sort(key=itemgetter("time"))
    logging.log(logging.DEBUG, "flatten anim - sorted keys: %s", flattened_keyframes)
    logging.log(logging.DEBUG, "flatten anim - cleanup start")
    # spriter treats each sprite's transformation separately, while synfig's spriteswitch layer doesn't. This means we've got to figure out the transformation data where there's nothing in synfig, by interpolating
    for flat_kf in enumerate(flattened_keyframes):
        i = flat_kf[0]
        wp = flat_kf[1]
        wp_time = wp["time"]
        # logging.log(logging.DEBUG, "i: %s", i)
        for il in anim_data["inner_layers"]:

            # ensure there's an entry for this layer in this frame
            layer_id = il["id"]
            if layer_id not in wp["layers"]:
                wp["layers"][layer_id] = {}

            layer_data = wp["layers"][layer_id]
            for anim_type in supported_anim_types:
                if anim_type not in layer_data:
                    # logging.log(logging.DEBUG, "anim type %s not found in frame %s for layer %s, lets get it elsewhere", anim_type, str(wp_time), layer_id)
                    # find the previous wp data with this layer's data in it
                    previous_wp_layerdata = {}
                    for j in range(i, -1, -1):
                        # logging.log(logging.DEBUG, "j: %s", j)
                        if layer_id in flattened_keyframes[j]["layers"] and anim_type in flattened_keyframes[j]["layers"][layer_id]:
                            previous_wp_layerdata = flattened_keyframes[j]["layers"][layer_id]
                            # logging.log(logging.DEBUG, "anim type %s found in frame %s for layer %s, lets use it", anim_type, str(flattened_keyframes[j]["time"]), layer_id)
                            # logging.log(logging.DEBUG, "%s", previous_wp_layerdata)
                            break

                        # logging.log(logging.DEBUG, "%s", flattened_keyframes[j]["layers"])

                    if anim_type == "spriteswitch":
                        # use sprite from previous wp
                        layer_data[anim_type] = copy.deepcopy(previous_wp_layerdata[anim_type])
                    elif anim_type == "pivot":
                        # use pivot from previous wp
                        # logging.log(logging.DEBUG, "flatten anim - previous pivot x: %s", (previous_wp_layerdata[anim_type]["x"]))
                        layer_data[anim_type] = copy.deepcopy(previous_wp_layerdata[anim_type])
                    else:
                        # interpolate! find next valid wp... if we can't find one, use previous valid data without interpolating
                        next_valid_wp = None
                        for j in range(i + 1, len(flattened_keyframes)):
                            if layer_id in flattened_keyframes[j]["layers"] and anim_type in flattened_keyframes[j]["layers"][layer_id]:
                                next_valid_wp = flattened_keyframes[j]
                                break
                        if next_valid_wp is not None:
                            # logging.log(logging.DEBUG, "flatten anim - interpolate %s", anim_type)
                            # logging.log(logging.DEBUG, "flatten anim - next valid wp time is %s", str(next_valid_wp["time"]))
                            time_delta = next_valid_wp["time"] - previous_wp_layerdata["time"]
                            next_wp_layerdata = next_valid_wp["layers"][layer_id]
                            layer_data[anim_type] = {}
                            if anim_type in ["offset", "scale"]:
                                for attr in ["x", "y"]:
                                    interp_ratio = (next_wp_layerdata[anim_type][attr] - previous_wp_layerdata[anim_type][attr]) / (time_delta)
                                    layer_data[anim_type][attr] = previous_wp_layerdata[anim_type][attr] + (interp_ratio * (wp_time - previous_wp_layerdata["time"]))
                            elif anim_type == "angle":
                                interp_ratio = (next_wp_layerdata[anim_type]["value"] - previous_wp_layerdata[anim_type]["value"]) / (time_delta)
                                layer_data[anim_type]["value"] = previous_wp_layerdata[anim_type]["value"] + (interp_ratio * (wp_time - previous_wp_layerdata["time"]))
                        else:
                            layer_data[anim_type] = copy.deepcopy(previous_wp_layerdata[anim_type])


    logging.log(logging.DEBUG, "flatten anim - cleanup end")
    # logging.log(logging.DEBUG, "flatten anim - result: %s", flattened_keyframes)
    return flattened_keyframes


def add_hierarchy_influence_to_flattened_keyframes(anim_data, flattened_keyframes):
    "synfig has a layer hierarchy, but spriter doesn't. We've got to apply parent edits to the children. This must be run after flattening the keyframes!"
    logging.log(logging.DEBUG, "flatten anim - apply hierarchy start")

    for flat_kf in enumerate(flattened_keyframes):
        i = flat_kf[0]
        wp = flat_kf[1]
        logging.log(logging.DEBUG, "i: %s", i)

        # start from the root layers of the anim, then process the children etc
        target_layer = get_anim_root_layer(anim_data)

        for child_layer_id in get_child_layers_ids(target_layer["id"], anim_data):
            apply_parent_transforms_to_wp_recursive(wp, get_layer_data_by_id(child_layer_id, anim_data), target_layer, anim_data)

    logging.log(logging.DEBUG, "flatten anim - apply hierarchy end")
    # logging.log(logging.DEBUG, "flatten anim - result: %s", flattened_keyframes)
    return flattened_keyframes

def apply_parent_transforms_to_wp_recursive(wp, this_layer_data, parent_layer_data, anim_data):
    "adds the parent's offset, scale etc to the target layer"
    if parent_layer_data is None:
        return

    wp_layer_data = wp["layers"][this_layer_data["id"]]
    wp_parent_layer_data = wp["layers"][parent_layer_data["id"]]

    logging.log(logging.DEBUG, "apply transforms from parent layer %s to layer %s", parent_layer_data["name"], this_layer_data["name"])

    for anim_type in supported_anim_types:
        if anim_type == "scale":
            for attr in ["x", "y"]:
                # logging.log(logging.DEBUG, "x scale before apply: %s", str(wp_layer_data[anim_type][attr]))
                wp_layer_data[anim_type][attr] *= wp_parent_layer_data[anim_type][attr]
                # logging.log(logging.DEBUG, "x scale after apply: %s", str(wp_layer_data[anim_type][attr]))
        elif anim_type == "angle":
            wp_layer_data[anim_type]["value"] += wp_parent_layer_data[anim_type]["value"]
        elif anim_type == "offset":
            # ok, this is where it gets a bit more complicated:
            # offset from parent is influenced by parent's offset, rotation and scale...
            # but the rotation and scaling are also influenced by the parent's pivot, as the children are rotated around that point
            logging.log(logging.DEBUG, "y offset before apply: %s", str(wp_layer_data[anim_type]["y"]))

            parent_pivot = wp_parent_layer_data["pivot"]
            parent_angle = wp_parent_layer_data["angle"]["value"]
            parent_angle_rads = math.radians(parent_angle)

            wp_layer_data[anim_type]["x"] -= parent_pivot["x"]
            wp_layer_data[anim_type]["y"] -= parent_pivot["y"]

            wp_layer_data[anim_type]["x"] *= wp_parent_layer_data["scale"]["x"]
            wp_layer_data[anim_type]["y"] *= wp_parent_layer_data["scale"]["y"]

            # logging.log(logging.DEBUG, "y offset after scale: %s", str(wp_layer_data[anim_type]["y"]))

            orig_x = wp_layer_data[anim_type]["x"]
            orig_y = wp_layer_data[anim_type]["y"]

            wp_layer_data[anim_type]["x"] = orig_x * math.cos(parent_angle_rads) - orig_y * math.sin(parent_angle_rads)
            wp_layer_data[anim_type]["y"] = orig_x * math.sin(parent_angle_rads) + orig_y * math.cos(parent_angle_rads)

            # logging.log(logging.DEBUG, "y offset after rotate: %s", str(wp_layer_data[anim_type]["y"]))

            wp_layer_data[anim_type]["x"] += wp_parent_layer_data[anim_type]["x"]
            wp_layer_data[anim_type]["y"] += wp_parent_layer_data[anim_type]["y"]

            # logging.log(logging.DEBUG, "y offset after translate: %s", str(wp_layer_data[anim_type]["y"]))

    # now, apply transforms to this layer's children!
    for child_layer_id in get_child_layers_ids(this_layer_data["id"], anim_data):
        apply_parent_transforms_to_wp_recursive(wp, get_layer_data_by_id(child_layer_id, anim_data), this_layer_data, anim_data)


def figure_out_anim_length(anim, flat_keyframes, context):
    "uses the time of the last frame, a named canvas keyframe or a fallback length"

    # prioritize finding a canvas keyframe with the right label
    anim_end_label = anim["name"] + "_end"
    for canvas_kf in context["canvas_keyframes"]:
        if canvas_kf["text"] == anim_end_label:
            return int(canvas_kf["time"] * 1000)


    return max(100, flat_keyframes[len(flat_keyframes) - 1]["time"])


def is_layer_visible_at_flat_keyframe(layer_data, anim_data, flat_kf):
    "returns true if this layer is visible, and therefore should be included in spriter's mainline this frame"
    #TODO consider other ways a sprite layer could be made invisible via animation in synfig?
    # check if this layer is child of a spriteswitch layer. if it is, check the spriteswitch layer's desired layer name. This layer should be hidden if the desired name isn't this one's
    # logging.log(logging.DEBUG, "is_layer_visible_at_flat_keyframe - layer id: %s, kf: %s", layer_data["id"], flat_kf)
    parent_layer = get_layer_data_by_id(layer_data["parent_layer_id"], anim_data)

    if parent_layer is not None and parent_layer["type"] == "switch":
        # get parent's desired sprite at this keyframe
        return flat_kf["layers"][layer_data["parent_layer_id"]]["spriteswitch"]["layer"] == layer_data["name"]

    return True


def write_data_to_xml(context):
    "writes the ingested data to the output file"

    out_root = ET.fromstring("""<?xml version="1.0" encoding="UTF-8"?>
<spriter_data scml_version="1.0" generator="BrashMonkey Spriter" generator_version="r11">
</spriter_data>
    """)
    logging.log(logging.DEBUG, out_root.tag)

    scml_folders = context["scml_folders"]
    scml_entities = context["scml_entities"]
    px_ratio = context["px_ratio"]

    for scmlfolder in scml_folders:
        folder_xml = ET.Element("folder", {"id":scmlfolder["id"],"name":scmlfolder["name"]})
        out_root.append(folder_xml)
        for folderfile in scmlfolder["files"]:
            file_xml = ET.Element("file", {k:folderfile[k] for k in ["id","name","width","height"]})
            file_xml.attrib["pivot_x"] = "0"
            file_xml.attrib["pivot_y"] = "1"
            folder_xml.append(file_xml)

        for ent in scml_entities:
            ent_xml = ET.Element("entity", {"id":"0","name":ent["name"]})
            for anim in ent["anims"]:
                anim_xml = ET.Element("animation", {"id":str(len(ent_xml)),"name":anim["name"],"interval":"100"}) #i don't know what interval is, but i've only seen it set to 100

                # anims have one mainline and multiple timeline tags.
                # the mainline tag declares all keyframes of the timelines, ordered by time. Every key entry has a list of all objects existing in that frame, even if nothing happened to them.
                # about the timelines tags, there seems to be one per object/sprite used, and, in each keyframe, time and all transformations and sprite link (folder+id) are declared.
                # this means most of the actual anim data is in the timelines.
                anim_mainline = ET.Element("mainline")
                anim_xml.append(anim_mainline)
                # we've got to "flatten" the synfig keyframes, because each animated data entry has their own timeline there
                flat_keyframes = flatten_synfig_anim_data(anim)

                anim_length = figure_out_anim_length(anim, flat_keyframes, context)
                anim_xml.attrib["length"] = str(anim_length)

                spriteobj_layers = [il for il in anim["inner_layers"] if il["is_spriter_object"]]

                # create timelines for each sprite element that should be shown in spriter
                sprite_timelines = {}
                for lso in spriteobj_layers:
                    timeline_xml = ET.Element("timeline", {"id":str(len(sprite_timelines)),"name":lso["name"] + "_" + lso["id"]})
                    sprite_timelines[lso["id"]] = timeline_xml
                    anim_xml.append(timeline_xml)


                for kf in flat_keyframes:

                    mainline_key_xml = ET.Element("key", {"id":str(len(anim_mainline)), "time":str(kf["time"])})

                    # for each possible sprite obj, figure out if it is visible in this frame;
                    # if it is, add its mainline entry, set up a timeline entry etc
                    for lso in spriteobj_layers:
                        if is_layer_visible_at_flat_keyframe(lso, anim, kf):

                            timeline_xml = sprite_timelines[lso["id"]]
                            objref_xml = ET.Element("object_ref", {"id":str(len(mainline_key_xml)), "timeline":timeline_xml.attrib["id"], "key":str(len(timeline_xml)), "z_index":"0"})
                            mainline_key_xml.append(objref_xml)

                            # add key to timeline
                            timeline_key_xml = ET.Element("key", {"id":str(len(timeline_xml)), "time":str(kf["time"]), "spin":"0"})

                            layer_sprite_data = lso["sprite_data"]
                            folder_id = layer_sprite_data["folder_id"]
                            file_id = layer_sprite_data["id"]

                            kflayer = kf["layers"][lso["id"]]
                            layer_offsets = lso["offsets"]
                            # convert pivot info to a value relative to the sprite's size and offsets
                            logging.log(logging.DEBUG, "layer pivot x before conv: %s", str(kflayer["pivot"]["x"]))
                            conv_pivot = kflayer["pivot"].copy()
                            conv_pivot["x"] = 0.5 + ((px_ratio * (conv_pivot["x"])) / float(lso["changed_width"]))
                            conv_pivot["y"] = 0.5 + ((px_ratio * (conv_pivot["y"])) / float(lso["changed_height"]))

                            timeline_obj_xml = ET.Element("object", {"folder":folder_id, "file":file_id, "x":str(px_ratio * (kflayer["offset"]["x"] + layer_offsets["offset"]["x"])), "y":str(px_ratio * (kflayer["offset"]["y"] + layer_offsets["offset"]["y"])), "scale_x":str(kflayer["scale"]["x"] * layer_offsets["scale"]["x"]), "scale_y":str(kflayer["scale"]["y"] * layer_offsets["scale"]["y"]), "angle":str(kflayer["angle"]["value"]), "pivot_x":str(conv_pivot["x"]), "pivot_y":str(conv_pivot["y"])})
                            timeline_key_xml.append(timeline_obj_xml)
                            timeline_xml.append(timeline_key_xml)


                    # if something visible happened in this keyframe, add the mainline key!
                    if len(mainline_key_xml) > 0:
                        anim_mainline.append(mainline_key_xml)

                ent_xml.append(anim_xml)
            out_root.append(ent_xml)

    logging.log(logging.DEBUG, "xml set up, writing now!")

    file_dest = context["file_dest"]
    with open(file_dest, "w", encoding="utf-8") as fil:
        xml_header = """<?xml version="1.0" encoding="UTF-8"?>
{content}
"""
        fil.write(xml_header.format(content=ET.tostring(out_root, "unicode")))



def process(passed_args):
    "the main data ingestion and exporting process!"

    file_to_export = passed_args.infile
    file_dest = passed_args.outfile

    sif_file_dir = os.path.dirname(file_to_export)

    # Read the input file
    tree = ET.parse(file_to_export)
    canvas = tree.getroot()  # canvas

    context = {} # any vars we need to pass around
    context["sif_file_dir"] = sif_file_dir
    context["file_dest"] = file_dest
    context["next_layer_id"] = 0
    scml_entities = []
    scml_folders = [] # we fill the folders as we find images in the sif file
    context["scml_entities"] = scml_entities
    context["scml_folders"] = scml_folders


    #fps = canvas.get("fps", 24)
    canvas_x = float(canvas.get("width", 500))
    #canvas_y = float(canvas.get("height", 500))
    canvas_viewbox = canvas.get("view-box", "-4.000000 2.250000 4.000000 -2.250000")
    # we can figure out the px-to-synfig units ratio using the obtained canvas dimensions
    viewbox = canvas_viewbox.split(" ")
    viewbox_width = abs(float(viewbox[0]) - float(viewbox[2]))

    context["px_ratio"] = canvas_x / viewbox_width

    # parse synfig keyframes... we may use them for knowing when an anim should end etc
    context["canvas_keyframes"] = []
    for canvas_kf in canvas.findall("keyframe"):
        new_kf = {}
        new_kf["time"] = float(canvas_kf.get("time").replace("s", "").replace("f", ""))
        new_kf["text"] = canvas_kf.text
        context["canvas_keyframes"].append(new_kf)
        # logging.log(logging.DEBUG, "add keyframe %s", new_kf["text"])


    scml_entity = {
        "name": "entity_000"
        }

    scml_entity["anims"] = []

    scml_entities.append(scml_entity)

    for child in canvas:
        # we're assuming each root layer in the sif file takes care of one spriter anim
        if child.tag == "layer":
            anim_name = child.get("desc")
            logging.log(logging.DEBUG, "anim_name: %s", anim_name)
            anim_data = {"name":anim_name}
            inner_layers = []
            anim_data["inner_layers"] = inner_layers

            process_layer_canvas(None, child, anim_data, context)

            scml_entity["anims"].append(anim_data)

    # done gathering data!
    # it's time to write it down in the out file
    # write folders and imgs...
    logging.log(logging.DEBUG, "done gathering data!")

    write_data_to_xml(context)

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
