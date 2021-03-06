"""
WhatsUP: astronomical object suggestions for Las Cumbres Observatory Global Telescope Network
Copyright (C) 2014-2015 LCOGT

views.py - data wrangling for templates

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""
import random
from datetime import datetime

from astropy import units as u
from astropy.coordinates import earth_orientation as earth
from astropy.time import Time
from django.conf import settings
from django.db.models import Q, Prefetch
from django.http import Http404
from numpy import sin, cos, arcsin, arccos, pi, arctan2, radians, degrees, floor
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.renderers import JSONRenderer, BrowsableAPIRenderer
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView
from rest_framework_jsonp.renderers import JSONPRenderer

from whatsup.models import Target, Params
from whatsup.serializers import TargetSerializer, TargetSerializerQuerystring, AdvTargetSerializer
from .utils import calc_lst, ra_sun, eqtohorizon

import logging

logger = logging.getLogger(__name__)

coords = settings.COORDS


@api_view(('GET',))
def api_root(request, format=None):
    return Response({
        'search': reverse('api_search', request=request, format=format),
    })


class TargetDetail(APIView):
    """
    Retrieve, update or delete a target instance.
    """

    def get_object(self, pk):
        try:
            return Target.objects.get(pk=pk)
        except Target.DoesNotExist:
            raise Http404

    def get(self, request, pk, format=None):
        target = self.get_object(pk)
        serializer = TargetSerializer(target)
        return Response(serializer.data)

    def put(self, request, pk, format=None):
        target = self.get_object(pk)
        serializer = TargetSerializer(target, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk, format=None):
        target = self.get_object(pk)
        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdvTargetListView(APIView):
    """
    A view that returns the list of Targets with advanced options for a given queryset.
    """
    renderer_classes = (JSONRenderer, JSONPRenderer, BrowsableAPIRenderer)

    def get(self, request, format=None):
        ser = TargetSerializerQuerystring(data=request.query_params)
        if not ser.is_valid(raise_exception=True):
            logger.error(ser.errors)
        targets = search_targets(request.query_params)
        serializer = AdvTargetSerializer(targets, many=True)
        content = {'targets': serializer.data,
                   'site': request.query_params.get('site', ''),
                   'datetime': request.query_params.get('start', ''), }
        return Response(content)

    def post(self, request, format=None):
        serializer = AdvTargetSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class TargetListView(APIView):
    """
    A view that returns the list of Targets for a given queryset.
    """
    renderer_classes = (JSONRenderer, JSONPRenderer, BrowsableAPIRenderer)

    def get(self, request, format=None):
        ser = TargetSerializerQuerystring(data=request.query_params)
        if not ser.is_valid(raise_exception=True):
            logger.error(ser.errors)
        targets = search_targets(request.query_params)
        serializer = TargetSerializer(targets, many=True)
        content = {'targets': serializer.data,
                   'site': request.query_params.get('site', ''),
                   'datetime': request.query_params.get('start', ''), }
        return Response(content)

    def post(self, request, format=None):
        serializer = TargetSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


def search_targets(query_params):
    if not query_params:
        return []
    site = query_params.get('site', '')
    start = query_params.get('start', '')
    end = query_params.get('end', '')
    callback = query_params.get('callback', '')
    full = query_params.get('full', '')
    s1 = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S") if start else None
    e1 = datetime.strptime(end, "%Y-%m-%dT%H:%M:%S") if end else None
    aperture = query_params.get('aperture', None)
    if query_params.get('colour'):
        colour = True
    else:
        colour = True
    if s1 and e1:
        # Find targets within a date range (i.e. not behind Sun during that time)
        meandate = s1 + (e1 - s1) / 2
        targets = targets_not_behind_sun(meandate, aperture)
        if full == 'messier':
            targets = targets.filter(name__startswith='M')
        elif full != 'true':
            if targets.count() > 30:
                targets = random.sample(targets, 30)
    else:
        # Find targets for only date/time given
        targets = visible_targets(start, site, aperture=aperture, colour=colour)
    return targets


def find_target(name):
    t = Target.objects.filter(name__icontains=name)
    if t.count() > 0:
        resp = {
            'name': t[0].name,
            'ra': t[0].ra,
            'dec': t[0].dec,
            'exp': t[0].exposure,
            'desc': t[0].description,
            'avmdesc': t[0].avm_desc,
            'avmcode': t[0].avm_code
        }
    else:
        resp = "'error' : 'Target not found.'"
    return resp


def targets_not_behind_sun(start, aperture=None, colour=True):
    ra = ra_sun(start)
    start = (ra - 4.) % 24
    end = (ra + 4.) % 24
    tgs = Target.objects.exclude(avm_desc='', ra__gte=start, ra__lte=end)
    if aperture:
        tgs = filter_targets_with_aperture(tgs, aperture)
    return tgs


def visible_targets(start, site, name=None, aperture=None, colour=True):
    """
    Produce a list of targets which visible to observer at specified date/time
    """
    # start=  "2014-07-21T14:00:00"
    # Find which targets are in the correct RA range, i.e. LST +/-2hours
    lst = calc_lst(start, site)
    s0 = float(((lst - 2.) * u.hourangle).to(u.degree) / u.deg)
    e0 = float(((lst + 2.) * u.hourangle).to(u.degree) / u.deg)
    tgs = Target.objects.filter(~Q(avm_desc=''), ra__gte=s0, ra__lte=e0).order_by('avm_desc')
    if aperture:
        tgs = filter_targets_with_aperture(tgs, aperture)
    targets = []
    # # Filter these targets by which are above (horizon + 30deg) for observer
    for t in tgs:
        hour = lst - float((t.ra * u.deg).to(u.hourangle) / u.hourangle)
        az, alt = eqtohorizon(hour, t.dec, coords[site]['lat'])
        if alt >= 30.:
            if aperture in ['0m4','1m0'] and (hour > 4.5 and hour < 5.5):
                # Hour angle limit of 5 for equatorial mounts
                continue
            targets.append(t)
    return tgs

def filter_targets_with_aperture(targets, aperture):
    """
    Filter queryset, prefetch related params while filtering them agains aperture parameter
    :param targets: Target queryset
    :param aperture: aperture parameter
    :return: queryset
    """
    prefetch = Prefetch('parameters', queryset=Params.objects.filter(aperture=aperture))
    return targets.filter(parameters__aperture=aperture).prefetch_related(prefetch).distinct()
